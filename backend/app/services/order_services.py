from __future__ import annotations

import base64
import binascii
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.app.core.security import session_claims
from backend.app.models.order import OrderAttachment, OrderHistoryEntry, OrderRecord, OrderUpdates
from backend.app.repositories import customer_repository, order_repository
from backend.app.schemas.user import normalize_email, validate_phone
from backend.app.services import auth_services, catalog_services
from backend.app.utils.clock import now_iso
from backend.app.utils.images import optimized_image


UPLOAD_DIR = order_repository.UPLOAD_DIR
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_FILES = 3
MAX_ORDER_FILES = 15
MAX_ORDER_UPLOAD_BYTES = 30 * 1024 * 1024
ALLOWED_UPLOADS = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", b"RIFF"),
    ".pdf": ("application/pdf", b"%PDF-"),
}
ORDER_STATUSES = {
    "Pending Confirmation",
    "Under Review",
    "Awaiting Quotation",
    "Awaiting Down Payment",
    "Draft Preparation",
    "Awaiting Customer Approval",
    "Approved for Printing",
    "In Production",
    "Ready for Pickup",
    "Completed",
    "Rejected",
    "Cancelled",
}
# Groups used by the customer portal rules and the admin dashboard.
PENDING_STATUSES = {"Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment"}
PROCESSING_STATUSES = {"Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production", "Ready for Pickup"}
CANCELLED_STATUSES = {"Cancelled", "Rejected"}
# Payments can be recorded once a quotation is accepted. Completed is included only so a balance left on an
# order completed before full payment was required can still be recorded (the balance check prevents overpaying).
PAYABLE_STATUSES = {"Awaiting Down Payment", "Completed"} | PROCESSING_STATUSES


def _text(value: Any, label: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise HTTPException(status_code=422, detail=f"{label} is required.")
    if len(value) > limit:
        raise HTTPException(status_code=422, detail=f"{label} must be {limit} characters or fewer.")
    return value


def _decode_upload(upload: Any) -> tuple[OrderAttachment, bytes]:
    if not isinstance(upload, dict):
        raise HTTPException(status_code=422, detail="Invalid reference file.")
    name = Path(_text(upload.get("name"), "File name", 180, True)).name
    extension = Path(name).suffix.lower()
    allowed = ALLOWED_UPLOADS.get(extension)
    if not allowed:
        raise HTTPException(status_code=422, detail="Reference files must be JPG, JPEG, PNG, WebP, or PDF.")
    mime_type, signature = allowed
    data_url = upload.get("dataUrl")
    if not isinstance(data_url, str) or not data_url.startswith(f"data:{mime_type};base64,"):
        raise HTTPException(status_code=422, detail=f"{name} has a file type that does not match its extension.")
    try:
        contents = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"{name} could not be decoded.") from error
    if not contents or len(contents) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{name} must be no larger than 5 MB.")
    if not contents.startswith(signature) or (extension == ".webp" and contents[8:12] != b"WEBP"):
        raise HTTPException(status_code=422, detail=f"{name} is not a valid {extension[1:].upper()} file.")
    metadata: OrderAttachment = {
        "id": secrets.token_hex(12),
        "name": name,
        "filename": f"{secrets.token_hex(16)}{extension}",
        "mime_type": mime_type,
        "size": len(contents),
    }
    return metadata, contents


def _decode_order_uploads(product: Any, reference_uploads: Any, field_uploads: Any) -> list[tuple[OrderAttachment, bytes]]:
    """Decode and check every uploaded file before anything is written to disk."""
    if reference_uploads is None:
        reference_uploads = []
    if not isinstance(reference_uploads, list) or len(reference_uploads) > MAX_REFERENCE_FILES:
        raise HTTPException(status_code=422, detail=f"Upload up to {MAX_REFERENCE_FILES} reference files.")
    decoded = [_decode_upload(upload) for upload in reference_uploads]
    if field_uploads is None:
        field_uploads = {}
    if not isinstance(field_uploads, dict):
        raise HTTPException(status_code=422, detail="Invalid product upload fields.")
    for key, uploads in field_uploads.items():
        field = catalog_services.file_field(product, key) if isinstance(key, str) else None
        if field is None:
            raise HTTPException(status_code=422, detail="A file was attached to an upload field this product does not have.")
        if not isinstance(uploads, list):
            raise HTTPException(status_code=422, detail=f"{field['label']}: invalid files.")
        if len(uploads) > field["max_files"]:
            raise HTTPException(status_code=422, detail=f"{field['label']}: attach up to {field['max_files']} file(s).")
        for upload in uploads:
            metadata, contents = _decode_upload(upload)
            metadata["field_key"] = field["field_key"]
            metadata["field_label"] = field["label"]
            decoded.append((metadata, contents))
    if len(decoded) > MAX_ORDER_FILES:
        raise HTTPException(status_code=422, detail=f"Attach up to {MAX_ORDER_FILES} files per order.")
    if sum(len(contents) for _, contents in decoded) > MAX_ORDER_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Combined uploads must be no larger than 30 MB. Email any extra originals to the shop.")
    return decoded


def _write_uploads(decoded: list[tuple[OrderAttachment, bytes]]) -> list[OrderAttachment]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[OrderAttachment] = []
    try:
        for metadata, contents in decoded:
            (UPLOAD_DIR / metadata["filename"]).write_bytes(contents)
            saved.append(metadata)
    except OSError as error:
        for metadata in saved:
            (UPLOAD_DIR / metadata["filename"]).unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="The uploaded files could not be saved.") from error
    return saved


def order_view(order: Mapping[str, Any], admin: bool = False) -> dict[str, Any]:
    result = dict(order)
    result.pop("customer_id", None)
    result.pop("idempotency_key", None)
    # Sent back with admin actions so an out-of-date screen cannot apply the same action twice.
    result["version"] = len(order["history"])
    product = dict(order["product"])
    product["image"] = optimized_image(product.get("image", ""))
    result["product"] = product
    prefix = f"/api/admin/orders/{order['reference']}/files" if admin else f"/api/orders/{order['reference']}/files"
    # Customers prove access to files with the order's email address; admins use their session.
    access = "" if admin else f"?email={quote(order['customer']['email'], safe='')}"

    def url(file_id: str) -> str:
        return f"{prefix}/{file_id}{access}"

    result.setdefault("pricing", {})
    result["attachments"] = [
        {
            "id": attachment["id"],
            "name": attachment["name"],
            "mime_type": attachment["mime_type"],
            "size": attachment["size"],
            "field_key": attachment.get("field_key", ""),
            "field_label": attachment.get("field_label", "Reference file"),
            "url": url(attachment["id"]),
        }
        for attachment in order["attachments"]
    ]
    if order["draft_attachment"]:
        draft = order["draft_attachment"]
        result["draft_attachment"] = {
            "id": draft["id"],
            "name": draft["name"],
            "mime_type": draft["mime_type"],
            "size": draft["size"],
            "url": url(draft["id"]),
        }
    return result


def order_after_update(order: OrderRecord | None, admin: bool = False) -> dict[str, Any]:
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order_view(order, admin)


def owned_order(reference: str, email: str) -> OrderRecord:
    order = order_repository.get_order(reference)
    if not order or not secrets.compare_digest(order["customer"]["email"].lower(), email.strip().lower()):
        raise HTTPException(status_code=404, detail="Order not found for that email address.")
    return order


def attachment_response(order: Mapping[str, Any], file_id: str, inline: bool = False) -> FileResponse:
    attachments = order["attachments"] + ([order["draft_attachment"]] if order["draft_attachment"] else [])
    attachment = next((item for item in attachments if item["id"] == file_id), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="File not found.")
    file_path = UPLOAD_DIR / attachment["filename"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="The stored file is unavailable.")
    return FileResponse(
        file_path,
        media_type=attachment["mime_type"],
        filename=attachment["name"],
        content_disposition_type="inline" if inline else "attachment",
    )


def submit_order(payload: dict[str, Any], request: Request, idempotency_key: str) -> tuple[dict[str, Any], bool]:
    idempotency_key = idempotency_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{20,128}", idempotency_key):
        raise HTTPException(status_code=422, detail="A valid Idempotency-Key header is required.")
    existing = order_repository.get_order_by_idempotency_key(idempotency_key)
    if existing is not None:
        return order_view(existing), True

    customer_payload = payload.get("customer")
    product_payload = payload.get("product")
    customization_payload = payload.get("customization")
    event_payload = payload.get("event")
    if not isinstance(customer_payload, dict):
        raise HTTPException(status_code=422, detail="Customer details are required.")
    if not isinstance(product_payload, dict):
        raise HTTPException(status_code=422, detail="Product details are required.")
    if not isinstance(customization_payload, dict):
        raise HTTPException(status_code=422, detail="Customization details are required.")
    if not isinstance(event_payload, dict):
        raise HTTPException(status_code=422, detail="Customer, product, customization, and event details are required.")
    try:
        email = normalize_email(_text(customer_payload.get("email"), "Email", 254, True))
        phone = validate_phone(_text(customer_payload.get("phone"), "Contact number", 40, True))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    # Orders are only accepted from signed-in customers so every order belongs to an account.
    claims = session_claims(request, "customer")
    if claims is None:
        raise HTTPException(status_code=401, detail="Sign in or create an account before submitting an order.")
    account = auth_services.get_customer(claims["sub"])
    if account is None:
        raise HTTPException(status_code=401, detail="The signed-in customer account is unavailable.")
    if email != account["email"]:
        raise HTTPException(status_code=403, detail="Use the email address from your signed-in account.")
    customer_id = account["id"]
    customer_payload = {
        "name": account["name"],
        "email": account["email"],
        "phone": phone,
        "messenger": account["messenger"],
    }
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=422, detail="Acknowledge the order instructions before submitting.")

    # The product, its form fields, and its prices come from the database, never from the client.
    product = catalog_services.orderable_product(product_payload.get("id"))
    decoded_uploads = _decode_order_uploads(product, payload.get("files", []), payload.get("field_files"))
    field_file_names: dict[str, list[str]] = {}
    for metadata, _ in decoded_uploads:
        if metadata.get("field_key"):
            field_file_names.setdefault(metadata["field_key"], []).append(metadata["name"])
    quantity, selections, specifications, pricing = catalog_services.evaluate_order(
        product,
        customization_payload.get("quantity"),
        customization_payload.get("options"),
        field_file_names,
    )
    notes = _text(customization_payload.get("notes", ""), "Special instructions", 2000)

    event_mode = product["event_mode"]
    has_event = event_mode == "required" or (event_mode == "optional" and payload.get("has_event") is True)
    event_details = {
        "celebrant": _text(event_payload.get("celebrant", ""), "Celebrant name", 120),
        "event_type": _text(event_payload.get("event_type", ""), "Event type", 120),
        "event_date": _text(event_payload.get("event_date", ""), "Event date", 20),
        "event_time": _text(event_payload.get("event_time", ""), "Event time", 20),
        "event_location": _text(event_payload.get("event_location", ""), "Event location", 240),
    }
    if has_event and (not event_details["celebrant"] or not event_details["event_type"]):
        raise HTTPException(status_code=422, detail="Celebrant name and event type are required for event orders.")
    if event_mode == "required" and not event_details["event_date"]:
        raise HTTPException(status_code=422, detail=f"Enter the event date for this {product['name']}.")
    if has_event and event_details["event_date"]:
        try:
            date.fromisoformat(event_details["event_date"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Enter a valid event date.") from error
    files = _write_uploads(decoded_uploads)

    customization: dict[str, Any] = {
        "quantity": quantity,
        "options": selections,
        "specifications": specifications,
        "notes": notes,
    }
    now = now_iso()
    estimate = pricing.get("estimated_total")
    history_entry: OrderHistoryEntry = {
        "at": now,
        "text": (
            f"Customer submitted an order request. Catalog estimate PHP {estimate:,.2f}; awaiting administrator confirmation."
            if estimate is not None
            else "Customer submitted an order request. Awaiting administrator quotation."
        ),
    }
    order: OrderRecord = {
        # Assigned by order_repository.create_order (XXX-MM-DD-YY), never by the client.
        "reference": "",
        "created_at": now,
        "updated_at": now,
        "customer": {
            "name": _text(customer_payload.get("name"), "Full name", 120, True),
            "email": email,
            "phone": phone,
            "messenger": _text(customer_payload.get("messenger", ""), "Messenger name", 120),
        },
        "product": {
            "id": product["id"],
            "category_id": product["category_id"],
            "name": product["name"],
            "category": product["category_label"],
            "description": product["description"],
            "image": product["image"],
        },
        "customization": customization,
        "event_details": event_details if has_event else {},
        "has_event": has_event,
        "acknowledged": True,
        "status": "Pending Confirmation",
        # The confirmed amount is set by the administrator's quotation; the catalog estimate is in pricing.
        "total_amount": None,
        "pricing": pricing,
        "payment_instructions": "",
        "payments": [],
        "attachments": files,
        "draft_attachment": None,
        "history": [history_entry],
        "admin_notes": "",
        "customer_id": customer_id,
    }
    try:
        order_repository.create_order(order, idempotency_key)
    except sqlite3.IntegrityError as error:
        for attachment in files:
            (UPLOAD_DIR / attachment["filename"]).unlink(missing_ok=True)
        existing = order_repository.get_order_by_idempotency_key(idempotency_key)
        if existing is not None:
            return order_view(existing), True
        raise HTTPException(status_code=409, detail="The order conflicted with an existing database record.") from error
    except Exception:
        for attachment in files:
            (UPLOAD_DIR / attachment["filename"]).unlink(missing_ok=True)
        raise
    return order_view(order), False


def customer_orders(email: str, reference: str) -> dict[str, Any]:
    normalized_email = _text(email, "Email", 254, True).lower()
    owned_order(reference, normalized_email)
    return {"orders": [order_view(order) for order in order_repository.list_orders(normalized_email)]}


def account_orders(customer_id: str) -> dict[str, Any]:
    return {"orders": [order_view(order) for order in order_repository.list_orders_for_customer(customer_id)]}


def account_order(customer_id: str, reference: str) -> dict[str, Any]:
    order = order_repository.get_order(reference)
    # Ownership is checked against the signed-in account; other customers' references look like missing orders.
    if order is None or order.get("customer_id") != customer_id:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order_view(order)


def get_customer_order(reference: str, email: str) -> dict[str, Any]:
    return order_view(owned_order(reference, email))


def get_customer_order_file(reference: str, file_id: str, email: str) -> FileResponse:
    return attachment_response(owned_order(reference, email), file_id)


def _save(
    order: OrderRecord,
    updates: OrderUpdates,
    event: str,
    expected_version: Any = None,
    admin: bool = False,
) -> dict[str, Any]:
    """Save an order change only if nobody changed the order since it was read.

    ``expected_version`` is the version the administrator's screen showed. When it is missing,
    the version read by this request is used, so concurrent duplicate actions are still refused.
    """
    version = order_repository.order_version(order)
    if expected_version is not None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise HTTPException(status_code=422, detail="Invalid order version.")
        version = expected_version
    try:
        updated = order_repository.update_order(order["reference"], updates, event, expected_version=version)
    except order_repository.StaleOrderError as error:
        raise HTTPException(
            status_code=409,
            detail="This order was changed by another action. Reload the order and try again.",
        ) from error
    return order_after_update(updated, admin=admin)


def approve_customer_draft(reference: str, email: Any) -> dict[str, Any]:
    order = owned_order(reference, _text(email, "Email", 254, True))
    if order["status"] != "Awaiting Customer Approval" or not order["draft_attachment"]:
        raise HTTPException(status_code=409, detail="There is no draft waiting for approval.")
    return _save(order, {"status": "Approved for Printing"}, "Customer approved the draft. The order may now move to production.")


def request_draft_changes(reference: str, email: Any, requested_changes: Any) -> dict[str, Any]:
    email = _text(email, "Email", 254, True)
    note = _text(requested_changes, "Requested changes", 1000, True)
    order = owned_order(reference, email)
    if order["status"] != "Awaiting Customer Approval":
        raise HTTPException(status_code=409, detail="There is no draft waiting for review.")
    return _save(order, {"status": "Draft Preparation", "admin_notes": note}, f"Customer requested draft changes: {note}")


def cancel_customer_order(reference: str, email: Any) -> dict[str, Any]:
    order = owned_order(reference, _text(email, "Email", 254, True))
    if order["status"] not in PENDING_STATUSES:
        raise HTTPException(status_code=409, detail="This order can no longer be cancelled from the customer portal.")
    if order["payments"]:
        raise HTTPException(status_code=409, detail="Contact the shop administrator to review an order with recorded payments.")
    return _save(order, {"status": "Cancelled"}, "Customer cancelled the order request before any payment was recorded.")


def admin_dashboard() -> dict[str, Any]:
    # Imported here: report_services imports this module for the shared status groups.
    from backend.app.services import report_services

    return report_services.dashboard()


def admin_orders(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from backend.app.services import report_services

    return report_services.order_list(dict(params or {}))


def admin_order(reference: str) -> dict[str, Any]:
    from backend.app.services import report_services

    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return report_services.admin_order_view(order)


def get_admin_order_file(reference: str, file_id: str) -> FileResponse:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    # Shown inside the admin image preview instead of being downloaded; never cached by shared caches.
    response = attachment_response(order, file_id, inline=True)
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _admin_order(reference: str) -> OrderRecord:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order


def paid_amount(order: Mapping[str, Any]) -> float:
    return round(sum(float(payment["amount"]) for payment in order["payments"]), 2)


def is_fully_paid(order: Mapping[str, Any]) -> bool:
    total = order["total_amount"]
    return total is not None and total > 0 and paid_amount(order) >= round(float(total), 2) - 0.005


def set_quotation(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = _admin_order(reference)
    if order["status"] not in {"Under Review", "Awaiting Quotation"}:
        raise HTTPException(status_code=409, detail="A quotation can only be set while the request is under review.")
    amount = payload.get("total_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 100000000:
        raise HTTPException(status_code=422, detail="Enter a valid total quotation amount.")
    instructions = _text(payload.get("payment_instructions", ""), "Payment instructions", 1000)
    return _save(
        order,
        {"total_amount": round(float(amount), 2), "payment_instructions": instructions, "status": "Awaiting Down Payment"},
        f"Administrator provided a quotation of PHP {float(amount):,.2f}. Payment instructions were {'provided' if instructions else 'not included'}.",
        payload.get("expected_version"),
        admin=True,
    )


def record_payment(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = _admin_order(reference)
    if order["status"] not in PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be recorded at this order stage.")
    amount = payload.get("amount")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before recording payment.")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    amount = round(float(amount), 2)
    balance = round(order["total_amount"] - paid_amount(order), 2)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    if amount > balance + 0.005:
        raise HTTPException(status_code=422, detail="Payment cannot exceed the remaining balance.")
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the payment was actually received.")
    payment = {
        "amount": amount,
        "method": _text(payload.get("method"), "Payment method", 60, True),
        "reference": _text(payload.get("reference", ""), "Payment reference", 120),
        "received_at": now_iso(),
    }
    payments = order["payments"] + [payment]
    updates: OrderUpdates = {"payments": payments}
    if order["status"] == "Awaiting Down Payment" and sum(item["amount"] for item in payments) >= order["total_amount"] * 0.5:
        updates["status"] = "Draft Preparation"
    return _save(
        order,
        updates,
        f"Administrator recorded PHP {payment['amount']:,.2f} received via {payment['method']}.",
        payload.get("expected_version"),
        admin=True,
    )


def upload_draft(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = _admin_order(reference)
    if order["status"] not in {"Draft Preparation", "Awaiting Customer Approval"}:
        raise HTTPException(status_code=409, detail="A draft can only be sent after preparation begins.")
    if order["total_amount"] is None or paid_amount(order) < order["total_amount"] * 0.5:
        raise HTTPException(status_code=409, detail="Record the required 50% down payment before sending a draft.")
    metadata, contents = _decode_upload(payload.get("file"))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (UPLOAD_DIR / metadata["filename"]).write_bytes(contents)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The draft file could not be saved.") from error
    try:
        return _save(
            order,
            {"draft_attachment": metadata, "status": "Awaiting Customer Approval"},
            f"Administrator attached draft '{metadata['name']}' for customer review. Printing is not approved.",
            payload.get("expected_version"),
            admin=True,
        )
    except HTTPException:
        (UPLOAD_DIR / metadata["filename"]).unlink(missing_ok=True)
        raise


# Status changes the administrator may make directly. Other moves happen through their own actions
# (quotation, payment, draft, customer approval).
ADMIN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "Pending Confirmation": {"Under Review", "Rejected", "Cancelled"},
    "Under Review": {"Awaiting Quotation", "Rejected", "Cancelled"},
    "Awaiting Quotation": {"Rejected", "Cancelled"},
    "Awaiting Down Payment": {"Rejected", "Cancelled"},
    "Approved for Printing": {"In Production"},
    "In Production": {"Ready for Pickup"},
    "Ready for Pickup": {"Completed"},
}


def update_order_status(reference: str, next_status: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    order = _admin_order(reference)
    next_status = _text(next_status, "Order status", 60, True)
    if next_status not in ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Select a valid order status.")
    if next_status not in ADMIN_STATUS_TRANSITIONS.get(order["status"], set()):
        raise HTTPException(status_code=409, detail=f"Cannot move an order from {order['status']} to {next_status}.")
    if next_status in CANCELLED_STATUSES and order["payments"]:
        raise HTTPException(status_code=409, detail="Orders with recorded payments need administrator review before rejection or cancellation.")
    if next_status == "Completed" and not is_fully_paid(order):
        raise HTTPException(
            status_code=409,
            detail="Record the remaining balance before marking this order as completed. Only fully paid orders can be completed.",
        )
    note = _text(payload.get("note", "") or "", "Reason", 500)
    updates: OrderUpdates = {"status": next_status}
    event = f"Administrator updated the order status to {next_status}."
    if note:
        event = f"{event} Reason: {note}"
        if next_status in CANCELLED_STATUSES:
            updates["admin_notes"] = note
    return _save(order, updates, event, payload.get("expected_version"), admin=True)
