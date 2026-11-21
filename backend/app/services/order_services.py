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

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.app.core.security import session_claims
from backend.app.models.order import (
    CUSTOMER_PAYMENT_METHOD,
    DELIVERY_FIELDS,
    DOWN_PAYMENT_RATE,
    FULFILLMENT_METHODS,
    NEW_ORDER_PAYMENT_OPTION,
    PAYMENT_OPTION_PENDING_LABEL,
    PAYMENT_OPTIONS,
    OrderAttachment,
    OrderHistoryEntry,
    OrderRecord,
    OrderUpdates,
)
from backend.app.repositories import audit_repository, customer_repository, notification_repository, order_repository
from backend.app.schemas.user import normalize_email, validate_phone
from backend.app.services import auth_services, catalog_services, design_services, notification_services, package_services
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
# Every order requires a 50% down payment, and only an administrator-verified payment confirms an order.
#
# Customized orders:  Pending Confirmation → Under Review → (Awaiting Quotation) → Quotation Sent
#   → the customer accepts (or declines → Quotation Declined → revised quotation)
#   → Awaiting Down Payment → the administrator verifies the 50% down payment → Confirmed
#   → Draft Preparation → Awaiting Customer Approval → Approved for Printing → In Production
#   → Ready for Pickup / Out for Delivery → Completed (fully paid).
# Fixed-price package orders use the published package total (saved with the order):
#   Pending Confirmation → (Under Review) → administrator approves and requests the down payment
#   → Awaiting Down Payment → verified 50% → Confirmed → … the same draft, production, and fulfillment steps.
# "Awaiting COD Confirmation" only exists on orders placed while Cash on Delivery was still offered.
ORDER_STATUSES = (
    "Pending Confirmation",
    "Under Review",
    "Awaiting Quotation",
    "Quotation Sent",
    "Quotation Declined",
    "Awaiting COD Confirmation",
    "Awaiting Down Payment",
    "Confirmed",
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
# Active orders: confirmed by a verified down payment and not finished yet.
PROCESSING_STATUSES = {"Confirmed", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production"} | RELEASE_STATUSES
DRAFTABLE_STATUSES = {"Confirmed", "Draft Preparation", "Awaiting Customer Approval"}
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
# Methods the administrator can record for money received directly ("Cash on Delivery" only for older COD orders).
PAYMENT_METHODS = ("GCash", "Maya", "Bank transfer", "Cash", "Cash on Delivery", "Other")
# Cash has no transaction number from a payment provider. The administrator records either the shop's
# official receipt number or, when none was issued, the system issues a cash receipt number; the payment is
# then identified by that number together with who confirmed receiving the money and when.
CASH_METHODS = frozenset({"Cash", "Cash on Delivery"})
# Verification states of payment entries (shown in Payments & Transactions).
VERIFICATION_VERIFIED = "Verified"
VERIFICATION_PENDING = "Pending Verification"
VERIFICATION_REJECTED = "Rejected"
VERIFICATION_MISSING = "Reference Missing"
REFERENCE_TYPE_LABELS = {
    "transaction": "Transaction reference",
    "official_receipt": "Official receipt no.",
    "system_cash_receipt": "Cash receipt (issued by system)",
}
COD_UNAVAILABLE = "Cash on Delivery is not available. Every order requires a 50% down payment."
ADMIN = "admin"
CUSTOMER = "customer"
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
    """"down_payment" (every new order), "cod" (older orders only), or "" (an older unaccepted quotation)."""
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
    """Cash on Delivery is no longer offered: every order requires the 50% down payment."""
    return False


def quotation_history(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = order.get("quotation_history")
    return [dict(item) for item in value] if isinstance(value, list) else []


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
    paid = paid_amount(order)
    pending = pending_submission_amount(order)
    cod = option == "cod"
    # Calculated on the server from the confirmed total only: a customized order has no down payment until
    # its quotation exists, and an estimate is never turned into an amount the customer must pay.
    required = 0.0 if cod else (round(float(total) * DOWN_PAYMENT_RATE, 2) if total is not None else None)
    if cod:
        status = "Not required (COD)"
    elif total is None:
        status = "Due after quotation"
    elif not price_agreed(order):
        status = "After quotation acceptance" if not is_fixed_price(order) else "After order approval"
    elif paid >= round(float(total) * DOWN_PAYMENT_RATE, 2) - 0.005:
        status = "Paid"
    elif pending > 0:
        status = "Pending Verification"
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
        # The other 50%: what is still to be paid after the down payment (everything, for older COD orders).
        "remaining_after_down_payment": round(float(total) - (required or 0.0), 2) if total is not None else None,
        # What the customer pays next and why; set by the server so the amount cannot be changed by the customer.
        "next_payment": next_customer_payment(order),
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
    result["design"] = customization.get("design")
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
        "previous_amount": stored_quote.get("previous_amount"),
        "down_payment": round(float(stored_quote["amount"]) * DOWN_PAYMENT_RATE, 2) if stored_quote.get("amount") is not None else None,
    } if stored_quote else None
    result["quotation_history"] = [
        {
            "revision": item.get("revision"),
            "amount": item.get("amount"),
            "previous_amount": item.get("previous_amount"),
            "note": item.get("note", ""),
            "sent_at": item.get("sent_at"),
            "status": item.get("status", ""),
            "responded_at": item.get("responded_at"),
            "decline_reason": item.get("decline_reason", ""),
            "superseded_at": item.get("superseded_at"),
            # Who sent it is for the shop's records only.
            **({"sent_by": item.get("sent_by", "")} if admin else {}),
        }
        for item in quotation_history(order)
    ]
    result["event_fields"] = _event_fields(order.get("event_details"))
    product = dict(order["product"])
    product["image"] = optimized_image(product.get("image", ""))
    result["product"] = product
    prefix = f"/api/admin/orders/{order['reference']}/files" if admin else f"/api/orders/{order['reference']}/files"

    # Both kinds of link are authorized by the session: the admin's, or the signed-in customer who owns the order.
    def url(file_id: str) -> str:
        return f"{prefix}/{file_id}"

    result.setdefault("pricing", {})
    # Who recorded or verified a payment is for the shop's records only.
    result["payments"] = [
        {
            **{key: value for key, value in payment.items() if admin or key not in {"recorded_by", "verification"}},
            "number": index,
            **({"verification_view": payment_verification(payment)} if admin else {}),
        }
        for index, payment in enumerate(order["payments"], start=1)
    ]
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
            "stage": item.get("stage", ""),
            "submitted_at": item.get("submitted_at"),
            "status": item.get("status", "pending"),
            "status_label": SUBMISSION_LABELS.get(item.get("status", "pending"), str(item.get("status", ""))),
            "verified_amount": item.get("verified_amount"),
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


SUBMISSION_LABELS = {"pending": "Pending Verification", "verified": "Verified", "rejected": "Rejected"}


def next_customer_payment(order: Mapping[str, Any]) -> dict[str, Any] | None:
    """The payment the customer can submit now, calculated by the server, or None when nothing can be paid.

    While the down payment is incomplete the amount is what remains of the 50% down payment; after that,
    the remaining balance. Customers never choose the amount: it always comes from the agreed total.
    """
    total = order.get("total_amount")
    if total is None or is_cod(order) or not payment_option(order) or order["status"] not in CUSTOMER_PAYABLE_STATUSES:
        return None
    paid = paid_amount(order)
    balance = round(float(total) - paid, 2)
    if balance <= 0.005:
        return None
    remaining_down = round(down_payment_due(order) - paid, 2)
    if remaining_down > 0.005:
        return {"stage": "down_payment", "label": "50% down payment", "amount": remaining_down}
    return {"stage": "balance", "label": "Remaining balance", "amount": balance}


def order_after_update(order: OrderRecord | None, admin: bool = False) -> dict[str, Any]:
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order_view(order, admin)


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
    # Orders are only accepted from signed-in customers so every order belongs to an account. This is
    # checked before the duplicate-submission lookup, so a reused key never reveals an order to a guest.
    claims = session_claims(request, "customer")
    if claims is None:
        raise HTTPException(status_code=401, detail="Sign in or create an account before submitting an order.")
    account = auth_services.get_customer(claims["sub"])
    if account is None:
        raise HTTPException(status_code=401, detail="The signed-in customer account is unavailable.")
    customer_id = account["id"]
    existing = order_repository.get_order_by_idempotency_key(idempotency_key)
    if existing is not None:
        return _replayed_order(existing, customer_id), True

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
    if email != account["email"]:
        raise HTTPException(status_code=403, detail="Use the email address from your signed-in account.")
    customer_payload = {
        "name": account["name"],
        "email": account["email"],
        "phone": phone,
        "messenger": account["messenger"],
    }
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=422, detail="Acknowledge the order instructions before submitting.")
    # Every order (package or customized) requires the 50% down payment; the customer does not choose.
    chosen_payment = payload.get("payment_option")
    if chosen_payment in (None, ""):
        chosen_payment = NEW_ORDER_PAYMENT_OPTION
    if chosen_payment == "cod":
        raise HTTPException(status_code=422, detail=COD_UNAVAILABLE)
    if chosen_payment != NEW_ORDER_PAYMENT_OPTION:
        raise HTTPException(status_code=422, detail="Invalid payment option. Every order requires a 50% down payment.")
    fulfillment_choice = clean_fulfillment(payload.get("fulfillment"))

    # The product, its form fields, its packages, and its prices come from the database, never from the client.
    product = catalog_services.orderable_product(product_payload.get("id"))
    has_event_fields = catalog_services.product_has_event_section(product)
    package_id = customization_payload.get("package_id")
    design_id = customization_payload.get("design_id")
    if package_id not in (None, "") and design_id not in (None, ""):
        raise HTTPException(status_code=422, detail="Choose either a package or a shop design, not both.")
    package = package_services.orderable_package(product, package_id) if package_id not in (None, "") else None
    design = None
    if package is None:
        # Packages are always orderable; customized and shop-design orders need the product's customization enabled.
        catalog_services.ensure_customizable(product)
        if design_id not in (None, ""):
            design = design_services.orderable_design(product, design_id)
    design_details: list[dict[str, str]] = []
    if design is not None:
        # The customer chose one of the shop's designs: no design or reference files are accepted.
        if payload.get("files") or payload.get("field_files"):
            raise HTTPException(status_code=422, detail="Shop design orders use the shop's design. Remove the uploaded files to continue.")
        design_details = design_services.personalization(design, customization_payload.get("design_details"))
    decoded_uploads = _decode_order_uploads(product, payload.get("files", []), payload.get("field_files"))
    field_file_names: dict[str, list[str]] = {}
    for metadata, _ in decoded_uploads:
        if metadata.get("field_key"):
            field_file_names.setdefault(metadata["field_key"], []).append(metadata["name"])
    quantity, selections, specifications, event_rows, pricing = catalog_services.evaluate_order(
        catalog_services.design_fields(product) if design is not None else product,
        customization_payload.get("quantity"),
        customization_payload.get("options"),
        field_file_names,
        package=package,
    )
    notes = _text(customization_payload.get("notes", ""), "Special instructions", 2000)

    if design is not None:
        # The design's personalization (names, date, venue, ...) is saved with the design snapshot.
        has_event = False
        event_details: dict[str, str] | list[dict[str, str]] = {}
    elif has_event_fields:
        # Invitation event details (celebrant, baptism, wedding) are catalog fields in the "event" section.
        has_event = True
        event_details = event_rows
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
        "order_kind": "package" if package else ("design" if design is not None else "customize"),
    }
    if design is not None:
        # Kept as ordered (image included) even if the design is later edited, replaced, or deleted.
        customization["design"] = design_services.order_snapshot(design, product, design_details)
    if package:
        customization["package"] = {
            "id": package["id"],
            "name": package["name"],
            "description": package["description"],
            "price": package["price"],
            "size": package["size"],
            "material": package["material"],
            "inclusions": package["inclusions"],
            "inclusion_details": package["inclusion_details"],
            "add_on_label": package["add_on_label"],
            "add_on_price": package["add_on_price"],
            "included_quantity": package["included_quantity"],
            "minimum_quantity": package["included_quantity"],
            "image": package["image"],
            "images": package["images"],
            # The computation used for this order, kept even if the package is edited later.
            "quantity": quantity,
            "additional_quantity": pricing.get("additional_quantity", 0),
            "additional_cost": pricing.get("additional_cost", 0.0),
            "computed_total": pricing.get("estimated_total"),
        }
    now = now_iso()
    estimate = pricing.get("estimated_total")
    # A package with a complete published total is a fixed-price order: its total is the package price and the
    # customer picks the payment option now. Anything else (customized orders, or a package whose options need a
    # quote) is priced by the administrator's quotation, and the payment option is chosen when it is accepted.
    fixed_price = bool(package) and isinstance(estimate, (int, float)) and estimate > 0 and not pricing.get("requires_quote")
    if fixed_price:
        customization["fixed_price"] = True
    if fixed_price:
        history_text = (
            f"Customer submitted a {package['name']} package order. Package total PHP {estimate:,.2f} "
            f"(50% down payment PHP {round(estimate * DOWN_PAYMENT_RATE, 2):,.2f}); awaiting administrator approval."
        )
    elif design is not None:
        history_text = (
            f"Customer submitted a shop design order (design “{design['name']}”)"
            + (f". Catalog estimate PHP {estimate:,.2f} (reference only)" if estimate is not None else "")
            + "; awaiting the administrator's quotation. A 50% down payment is due after the quotation is accepted."
        )
    elif estimate is not None:
        history_text = (
            f"Customer submitted an order request. Catalog estimate PHP {estimate:,.2f} (reference only); awaiting "
            "the administrator's quotation. A 50% down payment is due after the quotation is accepted."
        )
    else:
        history_text = (
            "Customer submitted an order request. Awaiting the administrator's quotation. "
            "A 50% down payment is due after the quotation is accepted."
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
        "quotation_history": [],
    }
    audit = {
        "action": "order.created",
        "actor_type": CUSTOMER,
        "actor_id": customer_id,
        "details": {
            "order_kind": customization["order_kind"],
            "fixed_price": fixed_price,
            "total": order["total_amount"],
            "down_payment": round(float(estimate) * DOWN_PAYMENT_RATE, 2) if fixed_price else None,
            "package": {
                "id": package["id"], "name": package["name"], "price": package["price"], "inclusions": package["inclusions"],
            } if package else None,
            "design": {"id": design["id"], "name": design["name"], "is_sample": design["is_sample"]} if design is not None else None,
        },
    }
    try:
        order_repository.create_order(order, idempotency_key, audit=audit)
    except sqlite3.IntegrityError as error:
        for attachment in files:
            (UPLOAD_DIR / attachment["filename"]).unlink(missing_ok=True)
        existing = order_repository.get_order_by_idempotency_key(idempotency_key)
        if existing is not None:
            return _replayed_order(existing, customer_id), True
        raise HTTPException(status_code=409, detail="The order conflicted with an existing database record.") from error
    except Exception:
        for attachment in files:
            (UPLOAD_DIR / attachment["filename"]).unlink(missing_ok=True)
        raise
    return order_view(order), False


def _replayed_order(existing: OrderRecord, customer_id: str) -> dict[str, Any]:
    """A repeated submission (same Idempotency-Key) returns the saved order only to the account that placed it.

    Another account reusing the key gets a conflict that says nothing about the existing order.
    """
    owner = existing.get("customer_id")
    if not owner or not secrets.compare_digest(str(owner), str(customer_id)):
        raise HTTPException(status_code=409, detail="This order request could not be submitted. Reload the page and try again.")
    return order_view(existing)


def account_orders(customer_id: str) -> dict[str, Any]:
    return {"orders": [order_view(order) for order in order_repository.list_orders_for_customer(customer_id)]}


def account_order(customer_id: str, reference: str) -> dict[str, Any]:
    # Ownership is checked against the signed-in account; other customers' references look like missing orders.
    return order_view(customer_owned_order(reference, None, customer_id))


def get_customer_order_file(customer_id: str, reference: str, file_id: str) -> FileResponse:
    """An order file (upload, draft, payment proof) for the signed-in customer who owns the order."""
    response = attachment_response(customer_owned_order(reference, None, customer_id), file_id)
    # Customer files are private: never stored by shared caches, never sniffed into another type.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _audit(action: str, actor_type: str, actor_id: Any = "", **details: Any) -> dict[str, Any]:
    return {"action": action, "actor_type": actor_type, "actor_id": str(actor_id or ""), "details": details}


def _save(
    order: OrderRecord,
    updates: OrderUpdates,
    event: str,
    expected_version: Any = None,
    admin: bool = False,
    *,
    audit: dict[str, Any] | None = None,
    notifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Save an order change only if nobody changed the order since it was read.

    ``expected_version`` is the version the administrator's screen showed. When it is missing,
    the version read by this request is used, so concurrent duplicate actions are still refused.
    The audit entry and customer notifications are saved in the same transaction as the change.
    """
    version = order_repository.order_version(order)
    if expected_version is not None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise HTTPException(status_code=422, detail="Invalid order version.")
        version = expected_version
    notifications = notifications or []
    try:
        updated = order_repository.update_order(
            order["reference"], updates, event, expected_version=version, audit=audit, notifications=notifications
        )
    except order_repository.StaleOrderError as error:
        raise HTTPException(
            status_code=409,
            detail="This order was changed by another action. Reload the order and try again.",
        ) from error
    if updated is not None:
        notification_services.deliver(order["reference"], notifications)
    return order_after_update(updated, admin=admin)


# ------------------------------------------------------------------ customer actions


def customer_owned_order(reference: str, email: Any, customer_id: str | None) -> OrderRecord:
    """The order, for a customer action. Changing an order needs the signed-in account that owns it.

    The email address alone (enough to look an order up) never authorizes accepting a quotation, submitting
    a payment, approving a draft, or cancelling. Orders placed before accounts existed have no owner, so the
    signed-in account's email must match the order's email.
    """
    if not customer_id:
        raise HTTPException(status_code=401, detail="Sign in to your customer account to make changes to this order.")
    order = order_repository.get_order(reference)
    not_found = HTTPException(status_code=404, detail="Order not found in your account.")
    if order is None:
        raise not_found
    order_email = order["customer"]["email"].strip().lower()
    if email not in (None, ""):
        if not secrets.compare_digest(order_email, _text(email, "Email", 254).lower()):
            raise not_found
    owner = order.get("customer_id")
    if owner:
        if not secrets.compare_digest(str(owner), str(customer_id)):
            raise not_found
    else:
        account = auth_services.get_customer(customer_id)
        if account is None or not secrets.compare_digest(account["email"].lower(), order_email):
            raise not_found
    return order


def approve_customer_draft(reference: str, email: Any, customer_id: str | None = None) -> dict[str, Any]:
    order = customer_owned_order(reference, email, customer_id)
    if order["status"] != "Awaiting Customer Approval" or not order["draft_attachment"]:
        raise HTTPException(status_code=409, detail="There is no draft waiting for approval.")
    return _save(
        order,
        {"status": "Approved for Printing"},
        "Customer approved the draft. The order may now move to production.",
        audit=_audit("draft.approved", CUSTOMER, customer_id, draft=order["draft_attachment"]["name"]),
    )


def request_draft_changes(reference: str, email: Any, requested_changes: Any, customer_id: str | None = None) -> dict[str, Any]:
    note = _text(requested_changes, "Requested changes", 1000, True)
    order = customer_owned_order(reference, email, customer_id)
    if order["status"] != "Awaiting Customer Approval":
        raise HTTPException(status_code=409, detail="There is no draft waiting for review.")
    return _save(
        order,
        {"status": "Draft Preparation", "admin_notes": note},
        f"Customer requested draft changes: {note}",
        audit=_audit("draft.changes_requested", CUSTOMER, customer_id, note=note),
    )


def cancel_customer_order(reference: str, email: Any, customer_id: str | None = None) -> dict[str, Any]:
    order = customer_owned_order(reference, email, customer_id)
    if order["status"] not in PENDING_STATUSES:
        raise HTTPException(status_code=409, detail="This order can no longer be cancelled from the customer portal.")
    if order["payments"] or pending_submissions(order):
        raise HTTPException(status_code=409, detail="Contact the shop administrator to review an order with recorded payments.")
    return _save(
        order,
        {"status": "Cancelled"},
        "Customer cancelled the order request before any payment was recorded.",
        audit=_audit("order.cancelled", CUSTOMER, customer_id),
    )


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


def _history_with_response(order: Mapping[str, Any], revision: int, changes: dict[str, Any]) -> list[dict[str, Any]]:
    """The quotation history with ``changes`` applied to the entry of ``revision``."""
    history = quotation_history(order)
    for entry in history:
        if entry.get("revision") == revision:
            entry.update(changes)
    return history


def accept_quotation(reference: str, email: Any, payload: Mapping[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """The customer accepts the latest quotation. The order then waits for the 50% down payment.

    Nothing is paid here: the down payment is submitted with a GCash receipt and verified by the administrator.
    An outdated revision is refused, so a customer never accepts a price they have not seen.
    """
    order = customer_owned_order(reference, email, customer_id)
    quote = _require_open_quotation(order)
    _quotation_revision(payload.get("quotation_revision"), order)
    option = payload.get("payment_option")
    if option == "cod":
        raise HTTPException(status_code=422, detail=COD_UNAVAILABLE)
    if option not in (None, "", NEW_ORDER_PAYMENT_OPTION):
        raise HTTPException(status_code=422, detail="Invalid payment option. Every order requires a 50% down payment.")
    total = float(order["total_amount"])
    if quote.get("amount") is not None and abs(float(quote["amount"]) - total) > 0.005:
        raise HTTPException(status_code=409, detail="The shop updated this quotation. Review the latest quotation before responding.")
    now = now_iso()
    quote.update({"status": "accepted", "responded_at": now})
    down = down_payment_due(order)
    return _save(
        order,
        {
            "quotation": quote,
            "quotation_history": _history_with_response(order, quote["revision"], {"status": "accepted", "responded_at": now}),
            "payment_option": NEW_ORDER_PAYMENT_OPTION,
            "status": "Awaiting Down Payment",
        },
        f"Customer accepted the quotation of PHP {total:,.2f} (revision {quote['revision']}). "
        f"50% down payment of PHP {down:,.2f} is due; the order is confirmed once the shop verifies it. "
        f"Remaining balance after the down payment: PHP {total - down:,.2f}.",
        audit=_audit("quotation.accepted", CUSTOMER, customer_id, revision=quote["revision"], amount=total, down_payment=down),
    )


def decline_quotation(reference: str, email: Any, payload: Mapping[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """The customer declines the quotation. A declined quotation can never be paid; the shop may revise it."""
    order = customer_owned_order(reference, email, customer_id)
    quote = _require_open_quotation(order)
    _quotation_revision(payload.get("quotation_revision"), order)
    reason = _text(payload.get("reason", "") or "", "Reason", 500)
    now = now_iso()
    quote.update({"status": "declined", "responded_at": now, "decline_reason": reason})
    event = f"Customer declined the quotation of PHP {float(order['total_amount']):,.2f} (revision {quote['revision']})."
    if reason:
        event += f" Reason: {reason}"
    return _save(
        order,
        {
            "quotation": quote,
            "quotation_history": _history_with_response(
                order, quote["revision"], {"status": "declined", "responded_at": now, "decline_reason": reason}
            ),
            "status": "Quotation Declined",
        },
        event,
        audit=_audit("quotation.declined", CUSTOMER, customer_id, revision=quote["revision"], amount=order["total_amount"], reason=reason),
    )


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


def submit_customer_payment(reference: str, email: Any, payload: Mapping[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """The customer submits a GCash payment: its reference number and the screenshot or receipt.

    The amount is calculated by the server (the remaining 50% down payment, then the remaining balance); the
    customer cannot choose or change it. The submission is saved as Pending Verification and never counts as
    paid, and never confirms the order, until the administrator verifies it. Only one submission can wait for
    verification at a time, which also stops double submissions.
    """
    order = customer_owned_order(reference, email, customer_id)
    if is_cod(order):
        raise HTTPException(status_code=409, detail="This order is Cash on Delivery: pay the shop when the order is delivered or picked up.")
    if order["status"] == "Quotation Declined":
        raise HTTPException(status_code=409, detail="This quotation was declined, so it cannot be paid. Wait for a revised quotation from the shop.")
    if order["status"] == "Quotation Sent":
        raise HTTPException(status_code=409, detail="Accept the quotation before paying the down payment.")
    if not payment_option(order) or order["total_amount"] is None or not price_agreed(order):
        raise HTTPException(status_code=409, detail="Payment opens after the quotation is accepted or the shop approves your package order.")
    if order["status"] not in CUSTOMER_PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be submitted at this order stage.")
    if pending_submissions(order):
        raise HTTPException(status_code=409, detail="A payment is already waiting for the shop to verify it.")
    due = next_customer_payment(order)
    if due is None:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    amount = payload.get("amount")
    if amount not in (None, ""):
        # The amount is fixed by the shop's agreed price. A different amount (an edited page, a stale screen) is refused.
        if not _valid_amount(amount) or abs(round(float(amount), 2) - due["amount"]) > 0.005:
            raise HTTPException(
                status_code=422,
                detail=f"The amount due is PHP {due['amount']:,.2f} ({due['label'].lower()}). It is calculated by the shop and cannot be changed.",
            )
    method = payload.get("method")
    if method in (None, ""):
        method = CUSTOMER_PAYMENT_METHOD
    if method != CUSTOMER_PAYMENT_METHOD:
        raise HTTPException(status_code=422, detail="Pay through GCash and upload the GCash screenshot or receipt.")
    reference_text = _text(payload.get("reference"), "GCash reference number", 120, True)
    if payload.get("proof") in (None, ""):
        raise HTTPException(status_code=422, detail="Upload the GCash payment screenshot or receipt.")
    _ensure_reference_unused(reference_text)
    proof, contents = _decode_upload(payload.get("proof"))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (UPLOAD_DIR / proof["filename"]).write_bytes(contents)
    except OSError as error:
        raise HTTPException(status_code=500, detail="The proof of payment could not be saved.") from error
    submission = {
        "id": secrets.token_hex(8),
        "amount": due["amount"],
        "stage": due["stage"],
        "method": method,
        "reference": reference_text,
        "proof": proof,
        "submitted_at": now_iso(),
        "status": "pending",
    }
    try:
        return _save(
            order,
            {"payment_submissions": submissions(order) + [submission]},
            f"Customer submitted a payment of PHP {due['amount']:,.2f} ({due['label'].lower()}) via {method} "
            f"(reference {reference_text}) with a screenshot/receipt. Pending verification by the administrator.",
            audit=_audit(
                "payment.submitted", CUSTOMER, customer_id,
                submission_id=submission["id"], amount=due["amount"], stage=due["stage"], method=method, reference=reference_text,
            ),
        )
    except HTTPException:
        (UPLOAD_DIR / proof["filename"]).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ administrator views


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


def admin_order_audit(reference: str) -> dict[str, Any]:
    _admin_order(reference)
    return {
        "audit": audit_repository.list_for_order(reference),
        "notifications": notification_repository.list_for_order(reference),
    }


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


# ------------------------------------------------------------------ quotation and package approval


def set_quotation(reference: str, payload: dict[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Send (or revise) the administrator's quotation for a customized order.

    The amount is always entered by the administrator; it is never calculated or taken from a package.
    Every quotation is kept in the quotation history (amount, previous amount, note, timestamps, the
    customer's response). Revising a quotation sends it again: an earlier unanswered or accepted revision
    is marked superseded, so only the latest revision can be accepted. The customer is notified each time.
    """
    order = _admin_order(reference)
    if is_fixed_price(order):
        raise HTTPException(status_code=409, detail="Package orders use the published package price. Approve the order instead of quoting it.")
    if order["status"] not in QUOTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="A quotation can only be set while the request is under review or waiting for payment.")
    if order["payments"] or pending_submissions(order):
        raise HTTPException(status_code=409, detail="The quotation cannot change after a payment was submitted or recorded.")
    amount = payload.get("total_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or not 0 < amount <= 100000000:
        raise HTTPException(status_code=422, detail="Enter a valid total quotation amount.")
    if payload.get("cod_allowed") is True:
        raise HTTPException(status_code=422, detail=COD_UNAVAILABLE)
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    note = _text(payload.get("note", "") or "", "Message to the customer", 1000)
    total = round(float(amount), 2)
    down = round(total * DOWN_PAYMENT_RATE, 2)
    previous = quotation(order)
    previous_amount = previous.get("amount")
    revision = int(previous.get("revision", 0)) + 1
    now = now_iso()
    history = quotation_history(order)
    if previous and not history:
        # An order quoted before the history existed: keep its earlier quotation as the first entry.
        history.append({
            "revision": previous.get("revision", 1),
            "amount": previous_amount,
            "previous_amount": None,
            "note": previous.get("note", ""),
            "sent_at": previous.get("sent_at"),
            "sent_by": "",
            "status": previous.get("status", "sent"),
            "responded_at": previous.get("responded_at"),
            "decline_reason": previous.get("decline_reason", ""),
        })
    for entry in history:
        if entry.get("status") in {"sent", "accepted"}:
            entry.update({"status": "superseded", "superseded_at": now})
    history.append({
        "revision": revision,
        "amount": total,
        "previous_amount": previous_amount,
        "note": note,
        "sent_at": now,
        "sent_by": actor,
        "status": "sent",
        "responded_at": None,
        "decline_reason": "",
    })
    quote = {
        "amount": total,
        "previous_amount": previous_amount,
        "cod_allowed": False,
        "revision": revision,
        "sent_at": now,
        "status": "sent",
        "note": note,
    }
    revised = bool(previous)
    if revised:
        event = f"Administrator sent a revised quotation of PHP {total:,.2f} (revision {revision}; previously PHP {float(previous_amount or 0):,.2f})."
    else:
        event = f"Administrator provided a quotation of PHP {total:,.2f} (revision {revision})."
    event += (
        f" 50% down payment PHP {down:,.2f}; remaining balance PHP {total - down:,.2f}. "
        f"Payment instructions were {'provided' if instructions else 'not included'}. Awaiting customer acceptance."
    )
    product = (order.get("product") or {}).get("name", "your order")
    if revised:
        title = "Revised quotation"
        message = (
            f"The shop revised the quotation for {product} from PHP {float(previous_amount or 0):,.2f} to PHP {total:,.2f}. "
            f"50% down payment: PHP {down:,.2f}. Review the latest quotation and accept or decline it in your customer portal."
        )
    else:
        title = "Quotation ready"
        message = (
            f"The shop sent a quotation of PHP {total:,.2f} for {product}. 50% down payment: PHP {down:,.2f}. "
            "Review it and accept or decline it in your customer portal."
        )
    if note:
        message += f" Message from the shop: {note}"
    return _save(
        order,
        {
            "total_amount": total,
            "payment_instructions": instructions,
            "status": "Quotation Sent",
            "quotation": quote,
            "quotation_history": history,
            "payment_option": NEW_ORDER_PAYMENT_OPTION,
        },
        event,
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "quotation.revised" if revised else "quotation.sent", ADMIN, actor,
            revision=revision, amount=total, previous_amount=previous_amount, down_payment=down, note=note,
        ),
        notifications=[notification_services.build(
            order, "quotation_revised" if revised else "quotation_sent", f"quotation:{revision}", title, message,
        )],
    )


def confirm_package_order(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Approve a fixed-price package order at its published total and request the 50% down payment.

    This is not the order confirmation: the order is confirmed only when the administrator verifies the down
    payment. Orders placed with Cash on Delivery while it was offered also switch to the down payment here.
    """
    order = _admin_order(reference)
    if not is_fixed_price(order):
        raise HTTPException(status_code=409, detail="Only fixed-price package orders are approved this way. Send a quotation instead.")
    if order["status"] not in {"Pending Confirmation", "Under Review"}:
        raise HTTPException(status_code=409, detail="This package order has already been approved or closed.")
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    total = float(order["total_amount"] or 0)
    down = down_payment_due(order)
    switched = " The order was placed with Cash on Delivery, which is no longer offered, so it now uses the 50% down payment." if is_cod(order) else ""
    shared = "provided" if instructions else "not included"
    package = (order.get("customization") or {}).get("package") or {}
    return _save(
        order,
        {"status": "Awaiting Down Payment", "payment_instructions": instructions, "payment_option": NEW_ORDER_PAYMENT_OPTION},
        f"Administrator approved the package order at PHP {total:,.2f}. A 50% down payment of PHP {down:,.2f} is required "
        f"to confirm the order.{switched} Payment instructions were {shared}.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("order.approved", ADMIN, actor, total=total, down_payment=down, package=package.get("name", "")),
        notifications=[notification_services.build(
            order, "down_payment_requested", "down-payment-requested", "Package order approved",
            f"The shop approved your {package.get('name') or 'package'} order (PHP {total:,.2f}). Pay the 50% down payment of "
            f"PHP {down:,.2f} through GCash and upload the screenshot or receipt in your customer portal to confirm your order.",
        )],
    )


# ------------------------------------------------------------------ payments


def _payment_kind(order: Mapping[str, Any], already_paid: float) -> str:
    if is_cod(order):
        return "COD collection"
    return "Down payment" if already_paid < down_payment_due(order) - 0.005 else "Balance payment"


def _payment_updates(order: OrderRecord, payment: dict[str, Any]) -> tuple[OrderUpdates, bool]:
    """The payment list with ``payment`` added, and whether it completes the down payment (confirming the order).

    Only verified money is passed here (a verified customer submission or money the administrator received),
    so an uploaded screenshot can never confirm an order by itself.
    """
    payments = order["payments"] + [payment]
    updates: OrderUpdates = {"payments": payments}
    confirmed = (
        order["status"] == "Awaiting Down Payment"
        and round(sum(float(item["amount"]) for item in payments), 2) >= down_payment_due(order) - 0.005
    )
    if confirmed:
        updates["status"] = "Confirmed"
    return updates, confirmed


def _payment_notification(order: OrderRecord, payment: dict[str, Any], confirmed: bool) -> dict[str, Any]:
    paid = round(paid_amount(order) + float(payment["amount"]), 2)
    balance = round(max(0.0, float(order["total_amount"] or 0) - paid), 2)
    if confirmed:
        return notification_services.build(
            order, "order_confirmed", "order-confirmed", "Down payment verified — order confirmed",
            f"We verified your 50% down payment of PHP {paid:,.2f}. Your order {order['reference']} is confirmed and is now "
            f"an active order. Remaining balance: PHP {balance:,.2f}.",
        )
    return notification_services.build(
        order, "payment_verified", f"payment-verified:{payment['id']}", "Payment verified",
        f"We verified your payment of PHP {float(payment['amount']):,.2f}. Remaining balance: PHP {balance:,.2f}.",
    )


def _check_payable(order: OrderRecord) -> float:
    """Common rules for recording money: the price is final and agreed, and money is due."""
    if order["status"] not in PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be recorded at this order stage.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before recording payment.")
    if not payment_option(order):
        raise HTTPException(status_code=409, detail="The customer must accept the quotation first.")
    balance = round(float(order["total_amount"]) - paid_amount(order), 2)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    return balance


CONFIRMED_SUFFIX = " The 50% down payment is verified: the order is now Confirmed."


def _cash_receipt_number() -> str:
    """A unique cash receipt number for cash received without an official receipt (CASH-YYYYMMDD-XXXXXX)."""
    today = now_iso()[:10].replace("-", "")
    return f"CASH-{today}-{secrets.token_hex(3).upper()}"


def _payment_reference(method: str, value: Any) -> tuple[str, str]:
    """The reference that identifies a payment the administrator records, and its type.

    GCash, Maya, bank transfers, and other providers always give a transaction/reference number, so it is
    required. Cash may have the shop's official receipt number; without one, the system issues a unique
    cash receipt number. Every reference can be recorded only once across all orders.
    """
    reference_text = _text(value or "", "Payment reference", 120)
    if method in CASH_METHODS:
        if reference_text:
            reference_type = "official_receipt"
        else:
            reference_text, reference_type = _cash_receipt_number(), "system_cash_receipt"
    else:
        if not reference_text:
            raise HTTPException(
                status_code=422,
                detail=f"Enter the {method} reference or transaction number. Payments through {method} must have a reference so they can be verified.",
            )
        if len(_payment_reference_key(reference_text)) < 4:
            raise HTTPException(status_code=422, detail="Enter the complete payment reference or transaction number.")
        reference_type = "transaction"
    _ensure_reference_unused(reference_text)
    return reference_text, reference_type


def payment_verification(payment: Mapping[str, Any]) -> dict[str, Any]:
    """How a recorded payment was verified, for the admin tables. Older payments may lack a reference."""
    stored = payment.get("verification") if isinstance(payment.get("verification"), dict) else {}
    reference_text = str(payment.get("reference") or "").strip()
    source = payment.get("source", "")
    reference_type = payment.get("reference_type") or ("transaction" if reference_text else "")
    if not reference_text:
        return {
            "status": VERIFICATION_MISSING,
            "reference_type": "",
            "reference_type_label": "No reference recorded",
            "detail": "Recorded before payment references were required. Add the receipt or transaction number.",
            "verified_by": payment.get("recorded_by", ""),
            "verified_at": payment.get("received_at"),
        }
    if source == "customer submission":
        detail = "Customer submission checked against the shop's account"
    elif reference_type == "system_cash_receipt":
        detail = "Cash received at the shop; receipt number issued by the system"
    elif reference_type == "official_receipt":
        detail = "Cash received; official receipt recorded"
    else:
        detail = "Received payment confirmed by the administrator"
    if stored.get("note"):
        detail += f" · {stored['note']}"
    return {
        "status": VERIFICATION_VERIFIED,
        "reference_type": reference_type,
        "reference_type_label": REFERENCE_TYPE_LABELS.get(reference_type, "Reference"),
        "detail": detail,
        "verified_by": stored.get("verified_by") or payment.get("recorded_by", ""),
        "verified_at": stored.get("verified_at") or payment.get("received_at"),
    }


def add_payment_reference(reference: str, payment_number: Any, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Attach the missing receipt or transaction number to an older payment that was saved without one.

    Only a payment with no reference can be changed; amounts and existing references are never edited.
    """
    order = _admin_order(reference)
    try:
        number = int(payment_number)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=404, detail="Payment not found.") from error
    if not 1 <= number <= len(order["payments"]):
        raise HTTPException(status_code=404, detail="Payment not found.")
    payments = [dict(item) for item in order["payments"]]
    payment = payments[number - 1]
    if str(payment.get("reference") or "").strip():
        raise HTTPException(status_code=409, detail="This payment already has a reference. Recorded references cannot be changed.")
    if payload.get("confirmed") is not True:
        raise HTTPException(status_code=422, detail="Confirm that you checked this reference against the shop's records.")
    method = str(payment.get("method") or "")
    reference_text, reference_type = _payment_reference(method if method in PAYMENT_METHODS else "Other", payload.get("reference"))
    note = _text(payload.get("note", "") or "", "Verification note", 300)
    now = now_iso()
    payment.update({
        "reference": reference_text,
        "reference_type": reference_type,
        "verification": {
            "status": "verified",
            "method": "admin_added_reference",
            "verified_by": actor,
            "verified_at": now,
            "note": note,
        },
    })
    return _save(
        order,
        {"payments": payments},
        f"Administrator added the missing {REFERENCE_TYPE_LABELS[reference_type].lower()} {reference_text} to payment "
        f"#{number} (PHP {float(payment.get('amount') or 0):,.2f} via {method or 'unknown method'})."
        + (f" Note: {note}" if note else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.reference_added", ADMIN, actor,
            payment_number=number, amount=float(payment.get("amount") or 0), reference=reference_text, reference_type=reference_type, note=note,
        ),
    )


def record_payment(reference: str, payload: dict[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Record money the shop received directly (cash at the shop, a transfer seen in the account)."""
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
    reference_text, reference_type = _payment_reference(method, payload.get("reference"))
    note = _text(payload.get("verification_note", "") or "", "Verification note", 300)
    now = now_iso()
    payment = {
        "id": secrets.token_hex(8),
        "amount": amount,
        "method": method,
        "reference": reference_text,
        "reference_type": reference_type,
        "received_at": now,
        "kind": _payment_kind(order, paid_amount(order)),
        "source": "admin",
        "recorded_by": actor,
        "verification": {
            "status": "verified",
            "method": "admin_cash_receipt" if method in CASH_METHODS else "admin_confirmed_reference",
            "verified_by": actor,
            "verified_at": now,
            "note": note,
        },
    }
    updates, confirmed = _payment_updates(order, payment)
    return _save(
        order,
        updates,
        f"Administrator recorded PHP {payment['amount']:,.2f} received via {payment['method']} "
        f"({REFERENCE_TYPE_LABELS[reference_type].lower()} {reference_text}; {payment['kind'].lower()})."
        + (f" Note: {note}" if note else "")
        + (CONFIRMED_SUFFIX if confirmed else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.recorded", ADMIN, actor,
            payment_id=payment["id"], amount=amount, method=method, reference=reference_text, reference_type=reference_type,
            kind=payment["kind"], confirmed=confirmed, note=note,
        ),
        notifications=[_payment_notification(order, payment, confirmed)],
    )


def _find_submission(order: OrderRecord, submission_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = [dict(item) for item in submissions(order)]
    found = next((item for item in items if item.get("id") == submission_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Payment submission not found.")
    if found.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"This payment submission was already {found.get('status')}.")
    return items, found


def verify_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Verify a payment the customer submitted. It becomes exactly one payment record and can never be verified twice.

    When it completes the 50% down payment, the order moves to Confirmed automatically and the customer is
    notified once that the down payment is verified and the order is confirmed.
    """
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
        "recorded_by": actor,
        "reference_type": "transaction",
        "verification": {
            "status": "verified",
            "method": "customer_submission",
            "verified_by": actor,
            "verified_at": now,
            "note": "",
        },
    }
    submission.update({
        "status": "verified", "reviewed_at": now, "reviewed_by": actor, "payment_id": payment["id"], "verified_amount": amount,
    })
    updates, confirmed = _payment_updates(order, payment)
    updates["payment_submissions"] = items
    reported = float(submission["amount"])
    adjusted = f" (customer reported PHP {reported:,.2f})" if abs(amount - reported) > 0.005 else ""
    return _save(
        order,
        updates,
        f"Administrator verified the customer's payment of PHP {amount:,.2f}{adjusted} via {payment['method']} "
        f"(reference {payment['reference']}) and recorded it as {payment['kind'].lower()}."
        + (CONFIRMED_SUFFIX if confirmed else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.verified", ADMIN, actor,
            submission_id=submission_id, payment_id=payment["id"], amount=amount, reported_amount=reported,
            reference=payment["reference"], kind=payment["kind"], confirmed=confirmed,
        ),
        notifications=[_payment_notification(order, payment, confirmed)],
    )


def reject_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    items, submission = _find_submission(order, submission_id)
    note = _text(payload.get("note", "") or "", "Reason", 500, True)
    submission.update({"status": "rejected", "reviewed_at": now_iso(), "reviewed_by": actor, "review_note": note})
    amount = float(submission["amount"])
    return _save(
        order,
        {"payment_submissions": items},
        f"Administrator could not verify the customer's payment of PHP {amount:,.2f} "
        f"(reference {submission.get('reference', '')}). Reason: {note}",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("payment.rejected", ADMIN, actor, submission_id=submission_id, amount=amount, reference=submission.get("reference", ""), reason=note),
        notifications=[notification_services.build(
            order, "payment_rejected", f"payment-rejected:{submission_id}", "Payment could not be verified",
            f"We could not verify your payment of PHP {amount:,.2f} (reference {submission.get('reference', '')}). Reason: {note} "
            "You can submit the payment again in your customer portal.",
        )],
    )


# ------------------------------------------------------------------ drafts, payment option, status


def upload_draft(reference: str, payload: dict[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    if order["status"] not in DRAFTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="A draft can only be sent after the order is confirmed.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before sending a draft.")
    if not down_payment_met(order):
        raise HTTPException(status_code=409, detail="Verify the required 50% down payment before sending a draft.")
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
            audit=_audit("draft.sent", ADMIN, actor, draft=metadata["name"], revised=kind == "revised draft"),
            notifications=[notification_services.build(
                order, "draft_ready", f"draft:{metadata['id']}", "Draft ready for your approval",
                f"The shop sent a {kind} for your order. Review it in your customer portal and approve it or request changes.",
            )],
        )
    except HTTPException:
        (UPLOAD_DIR / metadata["filename"]).unlink(missing_ok=True)
        raise


def payment_option_changeable(order: Mapping[str, Any]) -> bool:
    """An order placed with Cash on Delivery (no longer offered) can be switched to the 50% down payment
    until money is reported or received, or a draft is sent."""
    if not is_cod(order) or order["payments"] or pending_submissions(order):
        return False
    if order["status"] in {"Pending Confirmation", "Under Review", "Awaiting COD Confirmation"}:
        return True
    return order["status"] == "Draft Preparation" and not order["draft_attachment"]


def update_payment_option(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Switch an older Cash on Delivery order to the 50% down payment. Switching to COD is no longer possible."""
    order = _admin_order(reference)
    option = payload.get("payment_option")
    if option == "cod":
        raise HTTPException(status_code=422, detail=COD_UNAVAILABLE)
    if option != NEW_ORDER_PAYMENT_OPTION:
        raise HTTPException(status_code=422, detail="Choose the 50% down payment.")
    if option == payment_option(order):
        raise HTTPException(status_code=409, detail=f"This order already uses {PAYMENT_OPTIONS[option]}.")
    if not payment_option_changeable(order):
        raise HTTPException(
            status_code=409,
            detail="The payment option can only be changed before any payment is submitted or recorded and before a draft is sent.",
        )
    note = _text(payload.get("note", "") or "", "Reason", 500)
    updates: OrderUpdates = {"payment_option": option}
    event = f"Administrator changed the payment option to {PAYMENT_OPTIONS[option]}."
    if order["status"] in {"Draft Preparation", "Awaiting COD Confirmation"}:
        updates["status"] = "Awaiting Down Payment"
        event += " The 50% down payment is now required, so the order moved to Awaiting Down Payment."
    if note:
        event += f" Reason: {note}"
    return _save(
        order, updates, event, payload.get("expected_version"), admin=True,
        audit=_audit("payment.option_changed", ADMIN, actor, option_from=payment_option(order), option_to=option, note=note),
    )


# Status changes the administrator may make directly. Other moves happen through their own actions
# (quotation, customer acceptance, package approval, verified payment, draft, customer approval).
ADMIN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "Pending Confirmation": {"Under Review", "Rejected", "Cancelled"},
    "Under Review": {"Awaiting Quotation", "Rejected", "Cancelled"},
    "Awaiting Quotation": {"Rejected", "Cancelled"},
    "Quotation Sent": {"Rejected", "Cancelled"},
    "Quotation Declined": {"Rejected", "Cancelled"},
    "Awaiting COD Confirmation": {"Rejected", "Cancelled"},
    "Awaiting Down Payment": {"Rejected", "Cancelled"},
    "Confirmed": {"Draft Preparation"},
    "Approved for Printing": {"In Production"},
    "In Production": {"Ready for Pickup", "Out for Delivery"},
    "Ready for Pickup": {"Completed"},
    "Out for Delivery": {"Completed"},
}
# Status changes the customer is told about.
STATUS_NOTIFICATIONS = {
    "Ready for Pickup": ("Ready for pickup", "Your order is ready for pickup at the shop."),
    "Out for Delivery": ("Out for delivery", "Your order is out for delivery."),
    "Completed": ("Order completed", "Your order is completed. Thank you for ordering with ImPRESSion PrintShop!"),
    "Rejected": ("Order request rejected", "The shop could not accept your order request."),
    "Cancelled": ("Order cancelled", "The shop cancelled your order."),
}


def allowed_transitions(order: Mapping[str, Any]) -> list[str]:
    """The direct status moves that fit this order (its pricing and its delivery or pickup choice)."""
    allowed = set(ADMIN_STATUS_TRANSITIONS.get(order["status"], set()))
    if is_fixed_price(order):
        allowed.discard("Awaiting Quotation")
    if order["status"] == "In Production":
        allowed = {release_status(order)}
    return [status for status in ORDER_STATUSES if status in allowed]


def update_order_status(
    reference: str, next_status: Any, payload: Mapping[str, Any] | None = None, actor: str = ADMIN
) -> dict[str, Any]:
    payload = payload or {}
    order = _admin_order(reference)
    next_status = _text(next_status, "Order status", 60, True)
    if next_status not in ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Select a valid order status.")
    if next_status == "Confirmed":
        raise HTTPException(status_code=409, detail="An order is confirmed only by verifying its 50% down payment.")
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
    notifications = []
    if next_status in STATUS_NOTIFICATIONS:
        title, message = STATUS_NOTIFICATIONS[next_status]
        if note and next_status in CANCELLED_STATUSES:
            message += f" Reason: {note}"
        notifications.append(notification_services.build(order, "status_changed", f"status:{next_status}", title, message))
    return _save(
        order, updates, event, payload.get("expected_version"), admin=True,
        audit=_audit("order.status_changed", ADMIN, actor, note=note),
        notifications=notifications,
    )
