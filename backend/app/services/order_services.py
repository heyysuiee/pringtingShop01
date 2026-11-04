from __future__ import annotations

import base64
import binascii
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.app.code.security import session_claims
from backend.app.models.order import OrderAttachment, OrderHistoryEntry, OrderRecord, OrderUpdates
from backend.app.repositories import order_repository
from backend.app.services import auth_services


UPLOAD_DIR = order_repository.UPLOAD_DIR
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_UPLOADS = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
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
        raise HTTPException(status_code=422, detail="Reference files must be JPG, JPEG, PNG, or PDF.")
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
    if not contents.startswith(signature):
        raise HTTPException(status_code=422, detail=f"{name} is not a valid {extension[1:].upper()} file.")
    metadata: OrderAttachment = {
        "id": secrets.token_hex(12),
        "name": name,
        "filename": f"{secrets.token_hex(16)}{extension}",
        "mime_type": mime_type,
        "size": len(contents),
    }
    return metadata, contents


def _save_uploads(uploads: Any) -> list[OrderAttachment]:
    if uploads is None:
        return []
    if not isinstance(uploads, list) or len(uploads) > 3:
        raise HTTPException(status_code=422, detail="Upload up to 3 reference files.")
    decoded = [_decode_upload(upload) for upload in uploads]
    if sum(len(contents) for _, contents in decoded) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Combined reference files must be no larger than 10 MB.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        for metadata, contents in decoded:
            (UPLOAD_DIR / metadata["filename"]).write_bytes(contents)
            saved.append(metadata)
    except OSError as error:
        for metadata in saved:
            (UPLOAD_DIR / metadata["filename"]).unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="The reference files could not be saved.") from error
    return saved


def order_view(order: Mapping[str, Any], admin: bool = False) -> dict[str, Any]:
    result = dict(order)
    result.pop("customer_id", None)
    email = order["customer"]["email"]
    prefix = f"/api/admin/orders/{order['reference']}/files" if admin else f"/api/orders/{order['reference']}/files"
    result["attachments"] = [
        {
            "id": attachment["id"],
            "name": attachment["name"],
            "mime_type": attachment["mime_type"],
            "size": attachment["size"],
            "url": f"{prefix}/{attachment['id']}" if admin else f"{prefix}/{attachment['id']}?email={quote(email, safe='')}",
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
            "url": f"{prefix}/{draft['id']}" if admin else f"{prefix}/{draft['id']}?email={quote(email, safe='')}",
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


def attachment_response(order: Mapping[str, Any], file_id: str) -> FileResponse:
    attachments = order["attachments"] + ([order["draft_attachment"]] if order["draft_attachment"] else [])
    attachment = next((item for item in attachments if item["id"] == file_id), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="File not found.")
    file_path = UPLOAD_DIR / attachment["filename"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="The stored file is unavailable.")
    return FileResponse(file_path, media_type=attachment["mime_type"], filename=attachment["name"])


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
    email = _text(customer_payload.get("email"), "Email", 254, True).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    phone = _text(customer_payload.get("phone"), "Contact number", 40, True)
    if len(re.sub(r"\D", "", phone)) < 7:
        raise HTTPException(status_code=422, detail="Enter a valid contact number.")
    customer_id = None
    claims = session_claims(request, "customer")
    if claims:
        account = auth_services.get_customer(claims["sub"])
        if account is None:
            raise HTTPException(status_code=401, detail="The signed-in customer account is unavailable.")
        if email != account["email"]:
            raise HTTPException(status_code=403, detail="Use the email address from your signed-in account.")
        customer_id = account["id"]
        customer_payload = {
            "name": account["name"],
            "email": account["email"],
            "phone": account["phone"],
            "messenger": account["messenger"],
        }
    quantity = customization_payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or quantity > 50000:
        raise HTTPException(status_code=422, detail="Quantity must be between 1 and 50,000.")
    has_event = payload.get("has_event") is True
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=422, detail="Acknowledge the order instructions before submitting.")
    event_details = {
        "celebrant": _text(event_payload.get("celebrant", ""), "Celebrant name", 120),
        "event_type": _text(event_payload.get("event_type", ""), "Event type", 120),
        "event_date": _text(event_payload.get("event_date", ""), "Event date", 20),
        "event_time": _text(event_payload.get("event_time", ""), "Event time", 20),
        "event_location": _text(event_payload.get("event_location", ""), "Event location", 240),
    }
    if has_event and (not event_details["celebrant"] or not event_details["event_type"]):
        raise HTTPException(status_code=422, detail="Celebrant name and event type are required for event products.")
    if event_details["event_date"]:
        try:
            date.fromisoformat(event_details["event_date"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Enter a valid event date.") from error
    files = _save_uploads(payload.get("files", []))
    now = order_repository.now_iso()
    reference = f"IPS-{datetime.now(timezone.utc):%y%m%d}-{secrets.token_hex(16).upper()}"
    history_entry: OrderHistoryEntry = {
        "at": now,
        "text": "Customer submitted an order request. Awaiting administrator confirmation.",
    }
    order: OrderRecord = {
        "reference": reference,
        "created_at": now,
        "updated_at": now,
        "customer": {
            "name": _text(customer_payload.get("name"), "Full name", 120, True),
            "email": email,
            "phone": phone,
            "messenger": _text(customer_payload.get("messenger", ""), "Messenger name", 120),
        },
        "product": {
            "name": _text(product_payload.get("name"), "Product name", 160, True),
            "category": _text(product_payload.get("category"), "Product category", 120, True),
            "description": _text(product_payload.get("description", ""), "Product description", 600),
            "image": _text(product_payload.get("image", ""), "Product image", 200000),
        },
        "customization": {
            "size": _text(customization_payload.get("size"), "Size", 120, True),
            "quantity": quantity,
            "material": _text(customization_payload.get("material"), "Material or variant", 120, True),
            "details": _text(customization_payload.get("details", ""), "Customization details", 2000),
            "instructions": _text(customization_payload.get("instructions", ""), "Special instructions", 2000),
        },
        "event_details": event_details if has_event else {},
        "has_event": has_event,
        "acknowledged": True,
        "status": "Pending Confirmation",
        "total_amount": None,
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


def get_customer_order(reference: str, email: str) -> dict[str, Any]:
    return order_view(owned_order(reference, email))


def get_customer_order_file(reference: str, file_id: str, email: str) -> FileResponse:
    return attachment_response(owned_order(reference, email), file_id)


def approve_customer_draft(reference: str, email: Any) -> dict[str, Any]:
    order = owned_order(reference, _text(email, "Email", 254, True))
    if order["status"] != "Awaiting Customer Approval" or not order["draft_attachment"]:
        raise HTTPException(status_code=409, detail="There is no draft waiting for approval.")
    return order_after_update(
        order_repository.update_order(
            reference,
            {"status": "Approved for Printing"},
            "Customer approved the draft. The order may now move to production.",
        )
    )


def request_draft_changes(reference: str, email: Any, requested_changes: Any) -> dict[str, Any]:
    email = _text(email, "Email", 254, True)
    note = _text(requested_changes, "Requested changes", 1000, True)
    order = owned_order(reference, email)
    if order["status"] != "Awaiting Customer Approval":
        raise HTTPException(status_code=409, detail="There is no draft waiting for review.")
    return order_after_update(
        order_repository.update_order(
            reference,
            {"status": "Draft Preparation", "admin_notes": note},
            f"Customer requested draft changes: {note}",
        )
    )


def cancel_customer_order(reference: str, email: Any) -> dict[str, Any]:
    order = owned_order(reference, _text(email, "Email", 254, True))
    if order["status"] not in {"Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment"}:
        raise HTTPException(status_code=409, detail="This order can no longer be cancelled from the customer portal.")
    if order["payments"]:
        raise HTTPException(status_code=409, detail="Contact the shop administrator to review an order with recorded payments.")
    return order_after_update(
        order_repository.update_order(
            reference,
            {"status": "Cancelled"},
            "Customer cancelled the order request before any payment was recorded.",
        )
    )


def admin_orders() -> dict[str, Any]:
    return {"orders": [order_view(order, admin=True) for order in order_repository.list_orders()]}


def get_admin_order_file(reference: str, file_id: str) -> FileResponse:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return attachment_response(order, file_id)


def set_quotation(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if order["status"] not in {"Under Review", "Awaiting Quotation"}:
        raise HTTPException(status_code=409, detail="A quotation can only be set while the request is under review.")
    amount = payload.get("total_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 100000000:
        raise HTTPException(status_code=422, detail="Enter a valid total quotation amount.")
    instructions = _text(payload.get("payment_instructions", ""), "Payment instructions", 1000)
    return order_after_update(
        order_repository.update_order(
            reference,
            {"total_amount": round(float(amount), 2), "payment_instructions": instructions, "status": "Awaiting Down Payment"},
            f"Administrator provided a quotation of PHP {float(amount):,.2f}. Payment instructions were {'provided' if instructions else 'not included'}.",
        ),
        admin=True,
    )


def record_payment(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if order["status"] not in {
        "Awaiting Down Payment",
        "Draft Preparation",
        "Awaiting Customer Approval",
        "Approved for Printing",
        "In Production",
        "Ready for Pickup",
    }:
        raise HTTPException(status_code=409, detail="Payment cannot be recorded at this order stage.")
    amount = payload.get("amount")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before recording payment.")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    received = sum(payment["amount"] for payment in order["payments"])
    if amount > order["total_amount"] - received:
        raise HTTPException(status_code=422, detail="Payment cannot exceed the remaining balance.")
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the payment was actually received.")
    payment = {
        "amount": round(float(amount), 2),
        "method": _text(payload.get("method"), "Payment method", 60, True),
        "reference": _text(payload.get("reference", ""), "Payment reference", 120),
        "received_at": order_repository.now_iso(),
    }
    payments = order["payments"] + [payment]
    updates: OrderUpdates = {"payments": payments}
    if order["status"] == "Awaiting Down Payment" and sum(item["amount"] for item in payments) >= order["total_amount"] * 0.5:
        updates["status"] = "Draft Preparation"
    return order_after_update(
        order_repository.update_order(
            reference,
            updates,
            f"Administrator recorded PHP {payment['amount']:,.2f} received via {payment['method']}.",
        ),
        admin=True,
    )


def upload_draft(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    if order["status"] not in {"Draft Preparation", "Awaiting Customer Approval"}:
        raise HTTPException(status_code=409, detail="A draft can only be sent after preparation begins.")
    if order["total_amount"] is None or sum(item["amount"] for item in order["payments"]) < order["total_amount"] * 0.5:
        raise HTTPException(status_code=409, detail="Record the required 50% down payment before sending a draft.")
    metadata, contents = _decode_upload(payload.get("file"))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (UPLOAD_DIR / metadata["filename"]).write_bytes(contents)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The draft file could not be saved.") from error
    return order_after_update(
        order_repository.update_order(
            reference,
            {"draft_attachment": metadata, "status": "Awaiting Customer Approval"},
            f"Administrator attached draft '{metadata['name']}' for customer review. Printing is not approved.",
        ),
        admin=True,
    )


def update_order_status(reference: str, next_status: Any) -> dict[str, Any]:
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    next_status = _text(next_status, "Order status", 60, True)
    if next_status not in ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Select a valid order status.")
    allowed = {
        "Pending Confirmation": {"Under Review", "Rejected", "Cancelled"},
        "Under Review": {"Awaiting Quotation", "Rejected", "Cancelled"},
        "Awaiting Quotation": {"Rejected", "Cancelled"},
        "Awaiting Down Payment": {"Rejected", "Cancelled"},
        "Approved for Printing": {"In Production"},
        "In Production": {"Ready for Pickup"},
        "Ready for Pickup": {"Completed"},
    }
    if next_status not in allowed.get(order["status"], set()):
        raise HTTPException(status_code=409, detail=f"Cannot move an order from {order['status']} to {next_status}.")
    if next_status in {"Rejected", "Cancelled"} and order["payments"]:
        raise HTTPException(status_code=409, detail="Orders with recorded payments need administrator review before rejection or cancellation.")
    return order_after_update(
        order_repository.update_order(reference, {"status": next_status}, f"Administrator updated the order status to {next_status}."),
        admin=True,
    )
