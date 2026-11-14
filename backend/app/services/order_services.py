from __future__ import annotations

import base64
import binascii
import math
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
from backend.app.models.order import (
    DELIVERY_FIELDS,
    DOWN_PAYMENT_RATE,
    FULFILLMENT_METHODS,
    PAYMENT_OPTION_PENDING_LABEL,
    PAYMENT_OPTIONS,
    OrderAttachment,
    OrderHistoryEntry,
    OrderRecord,
    OrderUpdates,
)
from backend.app.repositories import customer_repository, order_repository
from backend.app.schemas.user import normalize_email, validate_phone
from backend.app.services import auth_services, catalog_services, mockup_services, package_services
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
# Every order status in workflow order.
#
# Customized orders:  Pending Confirmation → Under Review → (Awaiting Quotation) → Quotation Sent
#   → the customer accepts and picks a payment option (or declines → Quotation Declined → revised quotation)
#   → Awaiting Down Payment (verified 50%) or Awaiting COD Confirmation (administrator confirms COD)
#   → Draft Preparation → Awaiting Customer Approval → Approved for Printing → In Production
#   → Ready for Pickup / Out for Delivery → Completed (fully paid).
# Fixed-price package orders use the published package total and the payment option chosen at checkout:
#   Pending Confirmation → (Under Review) → administrator confirms → Awaiting Down Payment or, for COD,
#   Draft Preparation → … the same draft, production, and fulfillment steps.
ORDER_STATUSES = (
    "Pending Confirmation",
    "Under Review",
    "Awaiting Quotation",
    "Quotation Sent",
    "Quotation Declined",
    "Awaiting COD Confirmation",
    "Awaiting Down Payment",
    "Draft Preparation",
    "Awaiting Customer Approval",
    "Approved for Printing",
    "In Production",
    "Ready for Pickup",
    "Out for Delivery",
    "Completed",
    "Rejected",
    "Cancelled",
)
# Groups used by the customer portal rules and the admin dashboard.
PENDING_STATUSES = {
    "Pending Confirmation",
    "Under Review",
    "Awaiting Quotation",
    "Quotation Sent",
    "Quotation Declined",
    "Awaiting COD Confirmation",
    "Awaiting Down Payment",
}
RELEASE_STATUSES = {"Ready for Pickup", "Out for Delivery"}
PROCESSING_STATUSES = {"Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production"} | RELEASE_STATUSES
CANCELLED_STATUSES = {"Cancelled", "Rejected"}
# Payments can be recorded only after the price is final and the customer has chosen how to pay (a customized
# quotation must be accepted; a package order must be confirmed). Completed is included only so a balance left
# on an order completed before full payment was required can still be recorded (the balance check prevents overpaying).
PAYABLE_STATUSES = {"Awaiting Down Payment", "Completed"} | PROCESSING_STATUSES
# Customers may report a payment (reference + proof) while money is still due on a down-payment order.
CUSTOMER_PAYABLE_STATUSES = {"Awaiting Down Payment"} | PROCESSING_STATUSES
# A quotation can be sent or revised until money changes hands.
QUOTABLE_STATUSES = {
    "Under Review",
    "Awaiting Quotation",
    "Quotation Sent",
    "Quotation Declined",
    "Awaiting COD Confirmation",
    "Awaiting Down Payment",
}
PAYMENT_METHODS = ("GCash", "Maya", "Bank transfer", "Cash", "Cash on Delivery", "Other")
# While an order is in these statuses its total is only an offer (a quotation the customer has not accepted,
# or a package order the shop has not confirmed), so nothing is owed yet and reports do not count it.
PRICE_PENDING_STATUSES = {"Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined"}


def _valid_amount(value: Any) -> bool:
    """A positive, finite number of pesos. JSON parsing accepts NaN and Infinity, which every comparison
    would otherwise let through and which would corrupt the order's paid amount and balance."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


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


EVENT_LABELS = (
    ("celebrant", "Celebrant"),
    ("event_type", "Event type"),
    ("event_date", "Event date"),
    ("event_time", "Event time"),
    ("event_location", "Event location"),
)


def _event_fields(details: Any) -> list[dict[str, str]]:
    """Normalize either stored event shape into an ordered list of label/value rows."""
    if isinstance(details, list):
        return [
            {"label": str(item.get("label", "")), "value": str(item.get("value", ""))}
            for item in details
            if isinstance(item, dict) and item.get("value")
        ]
    if isinstance(details, dict):
        return [{"label": label, "value": str(details[key])} for key, label in EVENT_LABELS if details.get(key)]
    return []


def payment_option(order: Mapping[str, Any]) -> str:
    """"down_payment", "cod", or "" while a customized order has not accepted its quotation yet."""
    option = order.get("payment_option", "down_payment")
    if option == "":
        return ""
    return option if option in PAYMENT_OPTIONS else "down_payment"


def payment_option_label(order: Mapping[str, Any]) -> str:
    option = payment_option(order)
    return PAYMENT_OPTIONS[option] if option else PAYMENT_OPTION_PENDING_LABEL


def is_cod(order: Mapping[str, Any]) -> bool:
    return payment_option(order) == "cod"


def is_fixed_price(order: Mapping[str, Any]) -> bool:
    """A package order priced from the published package total when it was placed (no quotation needed)."""
    return bool((order.get("customization") or {}).get("fixed_price"))


def quotation(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("quotation")
    return dict(value) if isinstance(value, dict) else {}


def cod_allowed(order: Mapping[str, Any]) -> bool:
    """Package customers may always choose COD; a customized order only when the administrator permits it."""
    if is_fixed_price(order):
        return True
    return quotation(order).get("cod_allowed") is True


def fulfillment(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("fulfillment")
    return dict(value) if isinstance(value, dict) else {}


def fulfillment_method(order: Mapping[str, Any]) -> str:
    """"delivery" or "pickup". Orders placed before fulfillment was recorded were picked up at the shop."""
    method = fulfillment(order).get("method")
    return method if method in FULFILLMENT_METHODS else "pickup"


def release_status(order: Mapping[str, Any]) -> str:
    return "Out for Delivery" if fulfillment_method(order) == "delivery" else "Ready for Pickup"


def submissions(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = order.get("payment_submissions")
    return list(value) if isinstance(value, list) else []


def pending_submissions(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in submissions(order) if item.get("status") == "pending"]


def pending_submission_amount(order: Mapping[str, Any]) -> float:
    return round(sum(float(item.get("amount") or 0) for item in pending_submissions(order)), 2)


def price_agreed(order: Mapping[str, Any]) -> bool:
    """The total is owed: the customer accepted the quotation, or the shop confirmed the package order."""
    return order.get("total_amount") is not None and order["status"] not in PRICE_PENDING_STATUSES


def down_payment_due(order: Mapping[str, Any]) -> float:
    total = order.get("total_amount")
    return round(float(total) * DOWN_PAYMENT_RATE, 2) if total is not None else 0.0


def down_payment_met(order: Mapping[str, Any]) -> bool:
    """COD needs no down payment; otherwise verified payments must cover 50% of the confirmed total."""
    if order.get("total_amount") is None or not payment_option(order):
        return False
    return is_cod(order) or paid_amount(order) >= down_payment_due(order) - 0.005


def payment_breakdown(order: Mapping[str, Any]) -> dict[str, Any]:
    """Down payment, amount paid, and balance for one order, shown to customers and administrators.

    With the 50% down payment option, half of the confirmed total is due before the draft is prepared and
    the rest before release. With Cash on Delivery no down payment is required; the full amount is paid
    on delivery or pickup. Before the quotation the figures use the catalog/package estimate. Payments the
    customer reported but the administrator has not verified are shown separately and never count as paid.
    """
    option = payment_option(order)
    total = order.get("total_amount")
    estimate = (order.get("pricing") or {}).get("estimated_total")
    estimate = float(estimate) if isinstance(estimate, (int, float)) and not isinstance(estimate, bool) else None
    basis = float(total) if total is not None else estimate
    paid = paid_amount(order)
    pending = pending_submission_amount(order)
    cod = option == "cod"
    required = 0.0 if cod else (round(basis * DOWN_PAYMENT_RATE, 2) if basis is not None else None)
    if cod:
        status = "Not required (COD)"
    elif not option:
        status = "After quotation acceptance"
    elif total is None:
        status = "Due after quotation"
    elif paid >= round(float(total) * DOWN_PAYMENT_RATE, 2) - 0.005:
        status = "Paid"
    elif pending > 0:
        status = "Awaiting verification"
    elif paid > 0:
        status = "Partially paid"
    else:
        status = "Pending"
    balance = round(max(0.0, float(total) - paid), 2) if total is not None else None
    return {
        "payment_option": option,
        "payment_option_label": payment_option_label(order),
        "payment_option_chosen": bool(option),
        "cod_allowed": cod_allowed(order),
        "down_payment_rate": 0.0 if cod else DOWN_PAYMENT_RATE,
        "confirmed_total": round(float(total), 2) if total is not None else None,
        "estimated_total": estimate,
        "based_on_estimate": total is None and estimate is not None,
        "down_payment_required": required,
        "down_payment_remaining": round(max(0.0, required - paid), 2) if required is not None else None,
        "down_payment_status": status,
        "amount_paid": paid,
        "pending_verification": pending,
        "balance": balance,
        # What is still to be paid after the down payment (or everything, for COD), before the confirmed total.
        "remaining_after_down_payment": round(basis - (required or 0.0), 2) if basis is not None else None,
    }


def fulfillment_view(order: Mapping[str, Any]) -> dict[str, Any]:
    method = fulfillment_method(order)
    stored = fulfillment(order)
    rows = []
    if method == "delivery":
        rows = [{"key": key, "label": label, "value": str(stored.get(key) or "")} for key, label, _, _ in DELIVERY_FIELDS]
    return {
        "method": method,
        "label": FULFILLMENT_METHODS[method],
        # Orders placed before customers chose a fulfillment method default to store pickup.
        "recorded": bool(stored.get("method")),
        "address": rows,
        "release_status": release_status(order),
    }


def order_view(order: Mapping[str, Any], admin: bool = False) -> dict[str, Any]:
    result = dict(order)
    result.pop("customer_id", None)
    result.pop("idempotency_key", None)
    # Sent back with admin actions so an out-of-date screen cannot apply the same action twice.
    result["version"] = len(order["history"])
    customization = order.get("customization") or {}
    result["order_kind"] = customization.get("order_kind", "")
    result["package"] = customization.get("package")
    result["mockup"] = customization.get("mockup")
    result["payment_option"] = payment_option(order)
    result["payment"] = payment_breakdown(order)
    result["fixed_price"] = is_fixed_price(order)
    result["fulfillment"] = fulfillment_view(order)
    stored_quote = quotation(order)
    result["quotation"] = {
        "amount": stored_quote.get("amount"),
        "revision": stored_quote.get("revision", 0),
        "status": stored_quote.get("status", ""),
        "cod_allowed": stored_quote.get("cod_allowed") is True,
        "note": stored_quote.get("note", ""),
        "sent_at": stored_quote.get("sent_at"),
        "responded_at": stored_quote.get("responded_at"),
        "decline_reason": stored_quote.get("decline_reason", ""),
        "cod_confirmed_at": stored_quote.get("cod_confirmed_at"),
    } if stored_quote else None
    result["event_fields"] = _event_fields(order.get("event_details"))
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
    result["payment_submissions"] = [
        {
            "id": item["id"],
            "amount": item["amount"],
            "method": item.get("method", ""),
            "reference": item.get("reference", ""),
            "submitted_at": item.get("submitted_at"),
            "status": item.get("status", "pending"),
            "reviewed_at": item.get("reviewed_at"),
            "review_note": item.get("review_note", ""),
            "proof": {
                "name": item["proof"]["name"],
                "mime_type": item["proof"]["mime_type"],
                "size": item["proof"]["size"],
                "url": url(item["proof"]["id"]),
            } if item.get("proof") else None,
        }
        for item in submissions(order)
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
    attachments += [item["proof"] for item in submissions(order) if item.get("proof")]
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


def clean_fulfillment(value: Any) -> dict[str, str]:
    """Delivery or store pickup, chosen independently of the payment option.

    Deliveries need the full Philippine address (house number/street, barangay, city or municipality,
    province) plus the recipient's name and contact number; the landmark is optional. Pickup needs no
    address. A request without a fulfillment choice is treated as store pickup, as orders always were.
    """
    if value is None:
        return {"method": "pickup"}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Choose Delivery or Store Pickup.")
    method = value.get("method")
    if method not in FULFILLMENT_METHODS:
        raise HTTPException(status_code=422, detail="Choose Delivery or Store Pickup.")
    if method == "pickup":
        return {"method": "pickup"}
    cleaned = {"method": "delivery"}
    for key, label, required, limit in DELIVERY_FIELDS:
        raw = value.get(key, "")
        cleaned[key] = _text("" if raw is None else raw, label, limit, required)
    try:
        cleaned["contact_number"] = validate_phone(cleaned["contact_number"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"Delivery contact number: {error}") from error
    return cleaned


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
    chosen_payment = payload.get("payment_option")
    if chosen_payment in (None, ""):
        chosen_payment = "down_payment"
    if chosen_payment not in PAYMENT_OPTIONS:
        raise HTTPException(status_code=422, detail="Choose 50% down payment or Cash on Delivery (COD) as the payment option.")
    fulfillment_choice = clean_fulfillment(payload.get("fulfillment"))

    # The product, its form fields, its packages, and its prices come from the database, never from the client.
    product = catalog_services.orderable_product(product_payload.get("id"))
    has_event_fields = catalog_services.product_has_event_section(product)
    package_id = customization_payload.get("package_id")
    package = package_services.orderable_package(product, package_id) if package_id not in (None, "") else None
    decoded_uploads = _decode_order_uploads(product, payload.get("files", []), payload.get("field_files"))
    field_file_names: dict[str, list[str]] = {}
    for metadata, _ in decoded_uploads:
        if metadata.get("field_key"):
            field_file_names.setdefault(metadata["field_key"], []).append(metadata["name"])
    quantity, selections, specifications, event_rows, pricing = catalog_services.evaluate_order(
        product,
        customization_payload.get("quantity"),
        customization_payload.get("options"),
        field_file_names,
        package=package,
    )
    mockup = mockup_services.clean_mockup(product, customization_payload.get("mockup"), selections)
    if mockup:
        specifications.append({
            "key": "mockup",
            "label": "Invitation mockup",
            "value": f"{mockup['template_name']} template (colour option {mockup['palette'] + 1})",
            "type": "mockup",
        })
    notes = _text(customization_payload.get("notes", ""), "Special instructions", 2000)

    if has_event_fields:
        # Invitation event details (celebrant, baptism, wedding) are catalog fields in the "event" section.
        has_event = True
        event_details: dict[str, str] | list[dict[str, str]] = event_rows
    else:
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
        "order_kind": "package" if package else "customize",
    }
    if package:
        customization["package"] = {
            "id": package["id"],
            "name": package["name"],
            "description": package["description"],
            "price": package["price"],
            "size": package["size"],
            "material": package["material"],
            "inclusions": package["inclusions"],
            "add_on_label": package["add_on_label"],
            "add_on_price": package["add_on_price"],
            "included_quantity": package["included_quantity"],
            "image": package["image"],
            "images": package["images"],
        }
    if mockup:
        customization["mockup"] = mockup
    now = now_iso()
    estimate = pricing.get("estimated_total")
    # A package with a complete published total is a fixed-price order: its total is the package price and the
    # customer picks the payment option now. Anything else (customized orders, or a package whose options need a
    # quote) is priced by the administrator's quotation, and the payment option is chosen when it is accepted.
    fixed_price = bool(package) and isinstance(estimate, (int, float)) and estimate > 0 and not pricing.get("requires_quote")
    if fixed_price:
        customization["fixed_price"] = True
    else:
        chosen_payment = ""
    if fixed_price:
        history_text = (
            f"Customer submitted a {package['name']} package order. Package total PHP {estimate:,.2f}; "
            f"awaiting administrator confirmation. Payment option: {PAYMENT_OPTIONS[chosen_payment]}."
        )
    elif estimate is not None:
        history_text = (
            f"Customer submitted an order request. Catalog estimate PHP {estimate:,.2f}; awaiting administrator "
            "quotation. The payment option is chosen after the quotation is accepted."
        )
    else:
        history_text = (
            "Customer submitted an order request. Awaiting administrator quotation. "
            "The payment option is chosen after the quotation is accepted."
        )
    history_text += f" Fulfillment: {FULFILLMENT_METHODS[fulfillment_choice['method']]}."
    history_entry: OrderHistoryEntry = {"at": now, "text": history_text}
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
        # A fixed-price package order uses its published total; otherwise the administrator's quotation sets it.
        "total_amount": round(float(estimate), 2) if fixed_price else None,
        "pricing": pricing,
        "payment_instructions": "",
        "payments": [],
        "attachments": files,
        "draft_attachment": None,
        "history": [history_entry],
        "admin_notes": "",
        "customer_id": customer_id,
        "payment_option": chosen_payment,
        "fulfillment": fulfillment_choice,
        "quotation": {},
        "payment_submissions": [],
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
    if order["payments"] or pending_submissions(order):
        raise HTTPException(status_code=409, detail="Contact the shop administrator to review an order with recorded payments.")
    return _save(order, {"status": "Cancelled"}, "Customer cancelled the order request before any payment was recorded.")


def _quotation_revision(value: Any, order: Mapping[str, Any]) -> None:
    """The customer must respond to the quotation they actually saw, never to a newer revision."""
    current = quotation(order).get("revision", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value != current:
        raise HTTPException(
            status_code=409,
            detail="The shop updated this quotation. Review the latest quotation before responding.",
        )


def _require_open_quotation(order: Mapping[str, Any]) -> dict[str, Any]:
    quote = quotation(order)
    if order["status"] != "Quotation Sent" or quote.get("status") != "sent" or order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="There is no quotation waiting for your response.")
    return quote


def accept_quotation(reference: str, email: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The customer accepts the administrator's quotation and chooses how to pay.

    The 50% down payment is always available. Cash on Delivery is available only when the administrator
    allowed it for this order. Nothing is paid here: a down payment is reported and verified separately,
    and a COD arrangement still needs the administrator's confirmation.
    """
    order = owned_order(reference, _text(email, "Email", 254, True))
    quote = _require_open_quotation(order)
    _quotation_revision(payload.get("quotation_revision"), order)
    option = payload.get("payment_option")
    if option not in PAYMENT_OPTIONS:
        raise HTTPException(status_code=422, detail="Choose 50% Down Payment or Cash on Delivery (COD).")
    if option == "cod" and not cod_allowed(order):
        raise HTTPException(status_code=422, detail="Cash on Delivery is not available for this order. Choose the 50% down payment.")
    total = float(order["total_amount"])
    quote.update({"status": "accepted", "responded_at": now_iso()})
    if option == "cod":
        next_status = "Awaiting COD Confirmation"
        terms = f"Cash on Delivery: PHP {total:,.2f} is due on delivery or pickup. Awaiting administrator confirmation of the COD arrangement."
    else:
        next_status = "Awaiting Down Payment"
        terms = f"50% down payment of PHP {down_payment_due(order):,.2f} is due before the draft is prepared."
    return _save(
        order,
        {"quotation": quote, "payment_option": option, "status": next_status},
        f"Customer accepted the quotation of PHP {total:,.2f} and chose {PAYMENT_OPTIONS[option]}. {terms}",
    )


def decline_quotation(reference: str, email: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    order = owned_order(reference, _text(email, "Email", 254, True))
    quote = _require_open_quotation(order)
    _quotation_revision(payload.get("quotation_revision"), order)
    reason = _text(payload.get("reason", "") or "", "Reason", 500)
    quote.update({"status": "declined", "responded_at": now_iso(), "decline_reason": reason})
    event = f"Customer declined the quotation of PHP {float(order['total_amount']):,.2f}."
    if reason:
        event += f" Reason: {reason}"
    return _save(order, {"quotation": quote, "status": "Quotation Declined"}, event)


def _payment_reference_key(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).casefold()


def _ensure_reference_unused(reference_text: str, ignore_submission_id: str | None = None) -> None:
    """A payment reference (receipt or transaction number) can be recorded only once across all orders."""
    key = _payment_reference_key(reference_text)
    if not key:
        return
    for other in order_repository.list_orders():
        for payment in other["payments"]:
            if _payment_reference_key(str(payment.get("reference") or "")) == key:
                raise HTTPException(status_code=409, detail="This payment reference has already been recorded. Check the reference number.")
        for item in submissions(other):
            if item.get("id") == ignore_submission_id or item.get("status") == "rejected":
                continue
            if _payment_reference_key(str(item.get("reference") or "")) == key:
                raise HTTPException(status_code=409, detail="This payment reference has already been submitted. Check the reference number.")


def _payment_method(value: Any) -> str:
    method = _text(value, "Payment method", 60, True)
    if method not in PAYMENT_METHODS:
        raise HTTPException(status_code=422, detail=f"Choose a payment method: {', '.join(PAYMENT_METHODS)}.")
    return method


def submit_customer_payment(reference: str, email: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The customer reports a payment they sent (reference number and, optionally, a screenshot or receipt).

    The submission is only a claim: it does not count as paid until the administrator verifies it.
    Only one submission can wait for verification at a time, which also stops double submissions.
    """
    order = owned_order(reference, _text(email, "Email", 254, True))
    if not payment_option(order) or order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Payment opens after the quotation is accepted and a payment option is chosen.")
    if is_cod(order):
        raise HTTPException(status_code=409, detail="This order is Cash on Delivery: pay the shop when the order is delivered or picked up.")
    if order["status"] not in CUSTOMER_PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be submitted at this order stage.")
    if pending_submissions(order):
        raise HTTPException(status_code=409, detail="A payment is already waiting for the shop to verify it.")
    balance = round(float(order["total_amount"]) - paid_amount(order), 2)
    if balance <= 0.005:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    amount = payload.get("amount")
    if not _valid_amount(amount):
        raise HTTPException(status_code=422, detail="Enter the amount you paid.")
    amount = round(float(amount), 2)
    if amount > balance + 0.005:
        raise HTTPException(status_code=422, detail=f"The amount cannot exceed the remaining balance of PHP {balance:,.2f}.")
    method = _payment_method(payload.get("method"))
    reference_text = _text(payload.get("reference"), "Payment reference number", 120, True)
    _ensure_reference_unused(reference_text)
    proof: OrderAttachment | None = None
    if payload.get("proof") not in (None, ""):
        proof, contents = _decode_upload(payload.get("proof"))
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        try:
            (UPLOAD_DIR / proof["filename"]).write_bytes(contents)
        except OSError as error:
            raise HTTPException(status_code=500, detail="The proof of payment could not be saved.") from error
    submission = {
        "id": secrets.token_hex(8),
        "amount": amount,
        "method": method,
        "reference": reference_text,
        "proof": proof,
        "submitted_at": now_iso(),
        "status": "pending",
    }
    with_proof = " with proof of payment" if proof else ""
    try:
        return _save(
            order,
            {"payment_submissions": submissions(order) + [submission]},
            f"Customer submitted a payment of PHP {amount:,.2f} via {method} (reference {reference_text}){with_proof}. "
            "Awaiting administrator verification.",
        )
    except HTTPException:
        if proof:
            (UPLOAD_DIR / proof["filename"]).unlink(missing_ok=True)
        raise


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


def quotable(order: Mapping[str, Any]) -> bool:
    """A customized order's quotation can be sent or revised until any money is reported or received."""
    return (
        order["status"] in QUOTABLE_STATUSES
        and not is_fixed_price(order)
        and not order["payments"]
        and not pending_submissions(order)
    )


def set_quotation(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send (or revise) the administrator's quotation for a customized order.

    The amount is always entered by the administrator; it is never calculated. The customer must accept it
    and choose a payment option before any payment can be recorded. Revising a quotation sends it again,
    and any earlier acceptance or payment choice is cleared so the customer accepts the new terms.
    """
    order = _admin_order(reference)
    if is_fixed_price(order):
        raise HTTPException(status_code=409, detail="Package orders use the published package price. Confirm the order instead of quoting it.")
    if order["status"] not in QUOTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="A quotation can only be set while the request is under review or waiting for payment.")
    if order["payments"] or pending_submissions(order):
        raise HTTPException(status_code=409, detail="The quotation cannot change after a payment was submitted or recorded.")
    amount = payload.get("total_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount <= 100000000:
        raise HTTPException(status_code=422, detail="Enter a valid total quotation amount.")
    allow_cod = payload.get("cod_allowed")
    if allow_cod is None:
        allow_cod = False
    if not isinstance(allow_cod, bool):
        raise HTTPException(status_code=422, detail="Choose whether Cash on Delivery is allowed.")
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    note = _text(payload.get("note", "") or "", "Message to the customer", 1000)
    total = round(float(amount), 2)
    previous = quotation(order)
    revision = int(previous.get("revision", 0)) + 1
    quote = {
        "amount": total,
        "cod_allowed": allow_cod,
        "revision": revision,
        "sent_at": now_iso(),
        "status": "sent",
        "note": note,
    }
    verb = "sent a revised quotation" if previous else "provided a quotation"
    event = (
        f"Administrator {verb} of PHP {total:,.2f} (50% down payment PHP {total * DOWN_PAYMENT_RATE:,.2f}). "
        f"Cash on Delivery {'allowed' if allow_cod else 'not allowed'}. "
        f"Payment instructions were {'provided' if instructions else 'not included'}. Awaiting customer acceptance."
    )
    return _save(
        order,
        {
            "total_amount": total,
            "payment_instructions": instructions,
            "status": "Quotation Sent",
            "quotation": quote,
            # The customer picks the payment option again when accepting these terms.
            "payment_option": "",
        },
        event,
        payload.get("expected_version"),
        admin=True,
    )


def confirm_package_order(reference: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Confirm a fixed-price package order at its published total and share the payment instructions.

    A down-payment order then waits for its verified 50%; a COD order's arrangement is confirmed by this
    step, so its draft can be prepared right away.
    """
    order = _admin_order(reference)
    if not is_fixed_price(order):
        raise HTTPException(status_code=409, detail="Only fixed-price package orders are confirmed this way. Send a quotation instead.")
    if order["status"] not in {"Pending Confirmation", "Under Review"}:
        raise HTTPException(status_code=409, detail="This package order has already been confirmed or closed.")
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    total = float(order["total_amount"] or 0)
    if is_cod(order):
        next_status = "Draft Preparation"
        terms = f"Cash on Delivery confirmed: PHP {total:,.2f} is due on delivery or pickup."
    else:
        next_status = "Awaiting Down Payment"
        terms = f"50% down payment of PHP {down_payment_due(order):,.2f} is required before the draft is prepared."
    shared = "provided" if instructions else "not included"
    return _save(
        order,
        {"status": next_status, "payment_instructions": instructions},
        f"Administrator confirmed the package order at PHP {total:,.2f}. {terms} Payment instructions were {shared}.",
        payload.get("expected_version"),
        admin=True,
    )


def confirm_cod(reference: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The administrator confirms the Cash on Delivery arrangement the customer chose; drafting can start."""
    order = _admin_order(reference)
    if order["status"] != "Awaiting COD Confirmation" or not is_cod(order):
        raise HTTPException(status_code=409, detail="There is no Cash on Delivery arrangement waiting for confirmation.")
    if not cod_allowed(order):
        raise HTTPException(status_code=409, detail="Cash on Delivery is not allowed for this order. Revise the quotation instead.")
    quote = quotation(order)
    quote["cod_confirmed_at"] = now_iso()
    total = float(order["total_amount"] or 0)
    return _save(
        order,
        {"status": "Draft Preparation", "quotation": quote},
        f"Administrator confirmed the Cash on Delivery arrangement. PHP {total:,.2f} will be collected on delivery or pickup.",
        payload.get("expected_version"),
        admin=True,
    )


def _payment_kind(order: Mapping[str, Any], already_paid: float) -> str:
    if is_cod(order):
        return "COD collection"
    return "Down payment" if already_paid < down_payment_due(order) - 0.005 else "Balance payment"


def _payment_updates(order: OrderRecord, payment: dict[str, Any]) -> OrderUpdates:
    payments = order["payments"] + [payment]
    updates: OrderUpdates = {"payments": payments}
    if order["status"] == "Awaiting Down Payment" and sum(item["amount"] for item in payments) >= down_payment_due(order) - 0.005:
        updates["status"] = "Draft Preparation"
    return updates


def _check_payable(order: OrderRecord) -> float:
    """Common rules for recording money: the price is final, the payment option is chosen, and money is due."""
    if order["status"] not in PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be recorded at this order stage.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before recording payment.")
    if not payment_option(order):
        raise HTTPException(status_code=409, detail="The customer must accept the quotation and choose a payment option first.")
    balance = round(float(order["total_amount"]) - paid_amount(order), 2)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    return balance


def record_payment(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Record money the shop received directly (cash at pickup, COD collection, a transfer seen in the account)."""
    order = _admin_order(reference)
    balance = _check_payable(order)
    if pending_submissions(order):
        raise HTTPException(
            status_code=409,
            detail="The customer submitted a payment that is waiting for verification. Verify or reject it first so it is not recorded twice.",
        )
    amount = payload.get("amount")
    if not _valid_amount(amount):
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    amount = round(float(amount), 2)
    if amount > balance + 0.005:
        raise HTTPException(status_code=422, detail="Payment cannot exceed the remaining balance.")
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the payment was actually received.")
    method = _payment_method(payload.get("method"))
    reference_text = _text(payload.get("reference", "") or "", "Payment reference", 120)
    _ensure_reference_unused(reference_text)
    payment = {
        "id": secrets.token_hex(8),
        "amount": amount,
        "method": method,
        "reference": reference_text,
        "received_at": now_iso(),
        "kind": _payment_kind(order, paid_amount(order)),
        "source": "admin",
    }
    return _save(
        order,
        _payment_updates(order, payment),
        f"Administrator recorded PHP {payment['amount']:,.2f} received via {payment['method']} ({payment['kind'].lower()}).",
        payload.get("expected_version"),
        admin=True,
    )


def _find_submission(order: OrderRecord, submission_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = [dict(item) for item in submissions(order)]
    found = next((item for item in items if item.get("id") == submission_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Payment submission not found.")
    if found.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"This payment submission was already {found.get('status')}.")
    return items, found


def verify_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a payment the customer reported. It becomes exactly one payment record; it can never be verified twice."""
    order = _admin_order(reference)
    items, submission = _find_submission(order, submission_id)
    balance = _check_payable(order)
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the money was actually received before verifying.")
    amount = payload.get("amount")
    if amount in (None, ""):
        amount = submission["amount"]
    if not _valid_amount(amount):
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    amount = round(float(amount), 2)
    if amount > balance + 0.005:
        raise HTTPException(status_code=422, detail="Payment cannot exceed the remaining balance.")
    if any(payment.get("submission_id") == submission_id for payment in order["payments"]):
        raise HTTPException(status_code=409, detail="This payment submission was already recorded.")
    _ensure_reference_unused(str(submission.get("reference") or ""), ignore_submission_id=submission_id)
    now = now_iso()
    payment = {
        "id": secrets.token_hex(8),
        "amount": amount,
        "method": submission.get("method", ""),
        "reference": submission.get("reference", ""),
        "received_at": now,
        "kind": _payment_kind(order, paid_amount(order)),
        "source": "customer submission",
        "submission_id": submission_id,
    }
    submission.update({"status": "verified", "reviewed_at": now, "payment_id": payment["id"], "verified_amount": amount})
    updates = _payment_updates(order, payment)
    updates["payment_submissions"] = items
    reported = float(submission["amount"])
    adjusted = f" (customer reported PHP {reported:,.2f})" if abs(amount - reported) > 0.005 else ""
    return _save(
        order,
        updates,
        f"Administrator verified the customer's payment of PHP {amount:,.2f}{adjusted} via {payment['method']} "
        f"(reference {payment['reference']}) and recorded it as {payment['kind'].lower()}.",
        payload.get("expected_version"),
        admin=True,
    )


def reject_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    order = _admin_order(reference)
    items, submission = _find_submission(order, submission_id)
    note = _text(payload.get("note", "") or "", "Reason", 500, True)
    submission.update({"status": "rejected", "reviewed_at": now_iso(), "review_note": note})
    return _save(
        order,
        {"payment_submissions": items},
        f"Administrator could not verify the customer's payment of PHP {float(submission['amount']):,.2f} "
        f"(reference {submission.get('reference', '')}). Reason: {note}",
        payload.get("expected_version"),
        admin=True,
    )


def upload_draft(reference: str, payload: dict[str, Any]) -> dict[str, Any]:
    order = _admin_order(reference)
    if order["status"] not in {"Draft Preparation", "Awaiting Customer Approval"}:
        raise HTTPException(status_code=409, detail="A draft can only be sent after preparation begins.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before sending a draft.")
    if not down_payment_met(order):
        raise HTTPException(status_code=409, detail="Record the required 50% down payment before sending a draft.")
    metadata, contents = _decode_upload(payload.get("file"))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (UPLOAD_DIR / metadata["filename"]).write_bytes(contents)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The draft file could not be saved.") from error
    kind = "revised draft" if order["draft_attachment"] else "draft"
    try:
        return _save(
            order,
            {"draft_attachment": metadata, "status": "Awaiting Customer Approval"},
            f"Administrator attached {kind} '{metadata['name']}' for customer review. Printing is not approved.",
            payload.get("expected_version"),
            admin=True,
        )
    except HTTPException:
        (UPLOAD_DIR / metadata["filename"]).unlink(missing_ok=True)
        raise


def payment_option_changeable(order: Mapping[str, Any]) -> bool:
    """The payment option can change until money is reported or received, or a draft is sent."""
    if order["payments"] or pending_submissions(order) or not payment_option(order):
        return False
    if order["status"] in {"Pending Confirmation", "Under Review", "Awaiting COD Confirmation", "Awaiting Down Payment"}:
        return True
    return order["status"] == "Draft Preparation" and not order["draft_attachment"]


def update_payment_option(reference: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Switch an order between the 50% down payment and Cash on Delivery (for example at the customer's request).

    Switching to COD is the administrator's approval of COD for the order, so it also confirms the arrangement.
    """
    order = _admin_order(reference)
    option = payload.get("payment_option")
    if option not in PAYMENT_OPTIONS:
        raise HTTPException(status_code=422, detail="Choose 50% down payment or Cash on Delivery (COD).")
    if option == payment_option(order):
        raise HTTPException(status_code=409, detail=f"This order already uses {PAYMENT_OPTIONS[option]}.")
    if not payment_option_changeable(order):
        raise HTTPException(
            status_code=409,
            detail="The payment option can only be changed after the customer chose one, and before any payment is "
            "submitted or recorded and before a draft is sent.",
        )
    note = _text(payload.get("note", "") or "", "Reason", 500)
    updates: OrderUpdates = {"payment_option": option}
    event = f"Administrator changed the payment option to {PAYMENT_OPTIONS[option]}."
    quote = quotation(order)
    if option == "cod" and quote:
        quote["cod_allowed"] = True
        quote["cod_confirmed_at"] = now_iso()
        updates["quotation"] = quote
    if option == "cod" and order["status"] == "Awaiting Down Payment":
        updates["status"] = "Draft Preparation"
        event += " No down payment is required and the COD arrangement is confirmed, so the order moved to Draft Preparation."
    elif option == "down_payment" and order["status"] in {"Draft Preparation", "Awaiting COD Confirmation"}:
        updates["status"] = "Awaiting Down Payment"
        event += " The 50% down payment is now required, so the order moved to Awaiting Down Payment."
    if note:
        event += f" Reason: {note}"
    return _save(order, updates, event, payload.get("expected_version"), admin=True)


# Status changes the administrator may make directly. Other moves happen through their own actions
# (quotation, customer acceptance, package/COD confirmation, payment, draft, customer approval).
ADMIN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "Pending Confirmation": {"Under Review", "Rejected", "Cancelled"},
    "Under Review": {"Awaiting Quotation", "Rejected", "Cancelled"},
    "Awaiting Quotation": {"Rejected", "Cancelled"},
    "Quotation Sent": {"Rejected", "Cancelled"},
    "Quotation Declined": {"Rejected", "Cancelled"},
    "Awaiting COD Confirmation": {"Rejected", "Cancelled"},
    "Awaiting Down Payment": {"Rejected", "Cancelled"},
    "Approved for Printing": {"In Production"},
    "In Production": {"Ready for Pickup", "Out for Delivery"},
    "Ready for Pickup": {"Completed"},
    "Out for Delivery": {"Completed"},
}


def allowed_transitions(order: Mapping[str, Any]) -> list[str]:
    """The direct status moves that fit this order (its pricing and its delivery or pickup choice)."""
    allowed = set(ADMIN_STATUS_TRANSITIONS.get(order["status"], set()))
    if is_fixed_price(order):
        allowed.discard("Awaiting Quotation")
    if order["status"] == "In Production":
        allowed = {release_status(order)}
    return [status for status in ORDER_STATUSES if status in allowed]


def update_order_status(reference: str, next_status: Any, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    order = _admin_order(reference)
    next_status = _text(next_status, "Order status", 60, True)
    if next_status not in ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Select a valid order status.")
    if next_status not in allowed_transitions(order):
        if order["status"] == "In Production" and next_status in RELEASE_STATUSES:
            method = FULFILLMENT_METHODS[fulfillment_method(order)]
            raise HTTPException(status_code=409, detail=f"This order uses {method}: mark it {release_status(order)} instead.")
        raise HTTPException(status_code=409, detail=f"Cannot move an order from {order['status']} to {next_status}.")
    if next_status in CANCELLED_STATUSES and (order["payments"] or pending_submissions(order)):
        raise HTTPException(status_code=409, detail="Orders with recorded payments need administrator review before rejection or cancellation.")
    if next_status == "In Production":
        # Printing needs the customer's approved draft and the required payment confirmation.
        if not order["draft_attachment"]:
            raise HTTPException(status_code=409, detail="Printing needs a design draft approved by the customer.")
        if not down_payment_met(order):
            raise HTTPException(status_code=409, detail="Verify the required 50% down payment before production starts.")
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
