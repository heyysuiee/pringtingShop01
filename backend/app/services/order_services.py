from __future__ import annotations

import logging
import math
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from backend.app.core.config import settings
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
from backend.app.services import (
    auth_services,
    catalog_services,
    design_services,
    file_services,
    location_services,
    notification_services,
    ocr_services,
    package_services,
    payment_verification_services as verification_services,
    shop_settings_services,
)
from backend.app.utils.clock import now_iso


logger = logging.getLogger("printshop.payments")
from backend.app.utils.images import optimized_image


UPLOAD_DIR = order_repository.UPLOAD_DIR
# Payment proofs and drafts use the standard limit; order design files use the product's limit (larger for
# Tarpaulins), see file_services.
MAX_FILE_BYTES = settings.max_upload_mb * 1024 * 1024
MAX_REFERENCE_FILES = 3
MAX_ORDER_FILES = 15
ALLOWED_UPLOADS = file_services.ALLOWED_UPLOADS
# Every order status in workflow order.
#
# Every order requires a 50% down payment, and only an administrator-verified payment confirms an order.
#
# Customized orders:  Pending Confirmation → Under Review → (Awaiting Quotation) → Quotation Sent
#   → the customer accepts (or declines → Quotation Declined → revised quotation)
#   → Awaiting Down Payment → the administrator verifies the 50% down payment → Confirmed
#   → Draft Preparation → Awaiting Customer Approval → Approved for Printing → In Production
#   → Quality Checking → Packaging → (Awaiting Full Payment, skipped when already fully paid)
#   → Store pickup: Ready for Pickup → Order Claimed → Completed
#   → Delivery:     Ready for Dispatch → Out for Delivery → Delivered (or Delivery Failed → rescheduled
#                   → Ready for Dispatch) → Completed.
# Orders released before Quality Checking and Packaging existed (Ready for Pickup / Out for Delivery with no
# packaging record) can still be completed directly once fully paid.
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
    "Quality Checking",
    "Packaging",
    "Awaiting Full Payment",
    "Ready for Pickup",
    "Ready for Dispatch",
    "Out for Delivery",
    "Delivery Failed",
    "Delivered",
    "Order Claimed",
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
# Production stages (printing, inspection, packing). Printing needs the verified down payment and an approved draft.
PRODUCTION_STATUSES = {"In Production", "Quality Checking", "Packaging"}
# Fulfillment stages after packaging. New orders reach them only when fully paid.
RELEASE_STATUSES = {"Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed"}
# Active orders: confirmed by a verified down payment and not finished yet.
PROCESSING_STATUSES = (
    {"Confirmed", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "Awaiting Full Payment"}
    | PRODUCTION_STATUSES
    | RELEASE_STATUSES
)
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
# E-wallets added in Admin → Settings → Payment methods are accepted as well (see admin_payment_methods).
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


def _decode_upload(upload: Any, max_bytes: int = MAX_FILE_BYTES) -> tuple[OrderAttachment, bytes]:
    """A file sent inline as {name, dataUrl}: payment proofs, drafts, and order files from older clients."""
    metadata, contents = file_services.decode_data_url(upload, max_bytes)
    return metadata, contents  # type: ignore[return-value]


def _decode_receipt(upload: Any) -> tuple[OrderAttachment, bytes]:
    """A payment receipt: JPG/JPEG, PNG, or PDF, with the existing signature and size validation."""
    metadata, contents = _decode_upload(upload, MAX_FILE_BYTES)
    if metadata["mime_type"] not in ocr_services.RECEIPT_MIME_TYPES:
        raise HTTPException(status_code=422, detail="Upload the payment receipt as a JPG, PNG, or PDF file.")
    return metadata, contents


FileSource = bytes | str


def _order_file(customer_id: str, product: Any, upload: Any, field_key: str, max_bytes: int) -> tuple[OrderAttachment, FileSource]:
    """One order file: a completed chunked upload ({"upload_id"}) or an inline {name, dataUrl} file."""
    if isinstance(upload, dict) and upload.get("upload_id") not in (None, ""):
        metadata, upload_id = file_services.staged_upload(customer_id, upload.get("upload_id"), product, field_key, max_bytes)
        return metadata, upload_id  # type: ignore[return-value]
    return _decode_upload(upload, max_bytes)


def _decode_order_uploads(
    product: Any, reference_uploads: Any, field_uploads: Any, customer_id: str = ""
) -> list[tuple[OrderAttachment, FileSource]]:
    """Check every order file (type, signature, owner, size limit) before anything is stored."""
    per_file = catalog_services.upload_limit_bytes(product)
    if reference_uploads is None:
        reference_uploads = []
    if not isinstance(reference_uploads, list) or len(reference_uploads) > MAX_REFERENCE_FILES:
        raise HTTPException(status_code=422, detail=f"Upload up to {MAX_REFERENCE_FILES} reference files.")
    decoded = [_order_file(customer_id, product, upload, "", per_file) for upload in reference_uploads]
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
            metadata, source = _order_file(customer_id, product, upload, field["field_key"], per_file)
            metadata["field_key"] = field["field_key"]
            metadata["field_label"] = field["label"]
            decoded.append((metadata, source))
    if len(decoded) > MAX_ORDER_FILES:
        raise HTTPException(status_code=422, detail=f"Attach up to {MAX_ORDER_FILES} files per order.")
    combined = file_services.order_upload_limit_bytes(per_file)
    if sum(int(metadata["size"]) for metadata, _ in decoded) > combined:
        raise HTTPException(
            status_code=413,
            detail=f"Combined uploads must be no larger than {file_services.megabytes(combined)}. Email any extra originals to the shop.",
        )
    if len({source for _, source in decoded if isinstance(source, str)}) != sum(1 for _, source in decoded if isinstance(source, str)):
        raise HTTPException(status_code=422, detail="The same uploaded file was attached twice.")
    return decoded


def _write_uploads(decoded: list[tuple[OrderAttachment, FileSource]]) -> list[OrderAttachment]:
    return file_services.save_all(decoded)  # type: ignore[arg-type, return-value]


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


def price_basis(order: Mapping[str, Any]) -> str:
    """"package", "standard_size" (a size from the published price list), or "quotation"."""
    customization = order.get("customization") or {}
    if not is_fixed_price(order):
        return "quotation"
    return "standard_size" if customization.get("price_basis") == "standard_size" else "package"


def fixed_price_label(order: Mapping[str, Any]) -> str:
    return {"package": "Package total", "standard_size": "Price-list total"}.get(price_basis(order), "")


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
    """Where a packed, fully paid order goes next: Ready for Dispatch (delivery) or Ready for Pickup."""
    return "Ready for Dispatch" if fulfillment_method(order) == "delivery" else "Ready for Pickup"


def production(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("production")
    return json_copy(value) if isinstance(value, dict) else {}


def json_copy(value: Any) -> Any:
    """A deep copy of stored JSON data, so an update never changes the order that was read."""
    import json

    return json.loads(json.dumps(value))


def packaging_completed(order: Mapping[str, Any]) -> bool:
    return bool((production(order).get("packaging") or {}).get("completed_at"))


def legacy_release(order: Mapping[str, Any]) -> bool:
    """Released before Quality Checking and Packaging existed: completed directly once fully paid."""
    return order["status"] in {"Ready for Pickup", "Out for Delivery"} and not packaging_completed(order)


def delivery_fee(order: Mapping[str, Any]) -> float:
    """The delivery fee calculated by the server at checkout (0 for pickup and older orders)."""
    value = fulfillment(order).get("delivery_fee")
    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) and fulfillment_method(order) == "delivery" else 0.0


def cancellation_requests(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = order.get("cancellation_requests")
    return json_copy(value) if isinstance(value, list) else []


def pending_cancellation(order: Mapping[str, Any]) -> dict[str, Any] | None:
    return next((item for item in cancellation_requests(order) if item.get("status") == "pending_review"), None)


def ensure_no_pending_cancellation(order: Mapping[str, Any]) -> None:
    """While a cancellation request is reviewed, production, payment, and dispatch actions are on hold."""
    if pending_cancellation(order):
        raise HTTPException(
            status_code=409,
            detail="A cancellation request is waiting for review. Approve or reject it before continuing this order.",
        )


def release_updates(order: Mapping[str, Any], stored_fulfillment: dict[str, Any] | None = None) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Release a packed and fully paid order: its next status, the fulfillment record, and the customer notice.

    Store pickup orders get a one-time pickup code, which the claimant shows at the shop.
    """
    record = stored_fulfillment if stored_fulfillment is not None else fulfillment(order)
    record = json_copy(record)
    record.setdefault("method", fulfillment_method(order))
    now = now_iso()
    record["released_at"] = now
    status = release_status(order)
    if status == "Ready for Pickup":
        record["pickup_code"] = record.get("pickup_code") or secrets.token_hex(3).upper()
        location = record.get("pickup_location") or {}
        where = f" at {location['name']} ({location['address']})" if location.get("name") else " at the shop"
        notice = notification_services.build(
            order, "ready_for_pickup", "ready-for-pickup", "Ready for pickup",
            f"Your order {order['reference']} is fully paid and ready for pickup{where}. Show your pickup code "
            f"{record['pickup_code']} (also in your customer portal) when you claim it.",
        )
    else:
        notice = notification_services.build(
            order, "ready_for_dispatch", "ready-for-dispatch", "Ready for dispatch",
            f"Your order {order['reference']} is fully paid and packed. It will be handed to the courier soon; "
            "the tracking number will appear in your customer portal.",
        )
    return status, record, notice


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
    fee = delivery_fee(order)
    return {
        "delivery_fee": fee,
        # Product, package, and customization charges (the agreed total without the delivery fee).
        "subtotal": round(float(total) - fee, 2) if total is not None else None,
        "refunded": refunded_amount(order),
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


def fulfillment_view(order: Mapping[str, Any], admin: bool = False) -> dict[str, Any]:
    method = fulfillment_method(order)
    stored = fulfillment(order)
    rows = []
    if method == "delivery":
        rows = [{"key": key, "label": label, "value": str(stored.get(key) or "")} for key, label, _, _ in DELIVERY_FIELDS]
    from backend.app.services import fulfillment_services

    return {
        "method": method,
        "label": FULFILLMENT_METHODS[method],
        # Orders placed before customers chose a fulfillment method default to store pickup.
        "recorded": bool(stored.get("method")),
        "address": rows,
        "release_status": release_status(order),
        **fulfillment_services.fulfillment_details(order, admin),
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
    result["price_basis"] = price_basis(order)
    result["fixed_price_label"] = fixed_price_label(order)
    result["price_list"] = customization.get("price_list")
    result["fulfillment"] = fulfillment_view(order, admin)
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
        "delivery_fee": stored_quote.get("delivery_fee", 0.0),
        "total": quotation_total(stored_quote),
        "down_payment": round(quotation_total(stored_quote) * DOWN_PAYMENT_RATE, 2) if stored_quote.get("amount") is not None else None,
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
    result["payment_submissions"] = []
    for item in submissions(order):
        stored_verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        view = {
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
            "verification_status": item.get("verification_status") or stored_verification.get("status") or (
                "verified" if item.get("status") == "verified" else "rejected" if item.get("status") == "rejected" else "pending"
            ),
            "verification_status_label": stored_verification.get("status_label")
            or verification_services.STATUS_LABELS.get(
                item.get("verification_status") or ("verified" if item.get("status") == "verified" else "rejected" if item.get("status") == "rejected" else "pending"),
                "Pending Verification",
            ),
            "verification_method": item.get("verification_method") or stored_verification.get("verification_method", "manual"),
            "ocr": item.get("ocr") or {},
            "verification": stored_verification,
            # Customers see the note text and time in their payment history, but not the shop
            # actor's private account details that the full admin record carries.
            "admin_notes": item.get("admin_notes", []) if admin else [
                {"note": str(note.get("note") or ""), "at": str(note.get("at") or "")}
                for note in item.get("admin_notes", []) if isinstance(note, Mapping)
            ],
            "receipt_requests": item.get("receipt_requests", []) if admin else [
                {"at": str(receipt_request.get("at") or ""), "note": str(receipt_request.get("note") or "")}
                for receipt_request in item.get("receipt_requests", []) if isinstance(receipt_request, Mapping)
            ],
            "required_amount": item.get("amount"),
            "proof": {
                "name": item["proof"]["name"],
                "mime_type": item["proof"]["mime_type"],
                "size": item["proof"]["size"],
                "url": url(item["proof"]["id"]),
            } if item.get("proof") else None,
        }
        if admin:
            view["comparison"] = verification_services.comparison_view(order, view, stored_verification or {"status": view["verification_status"]})
        result["payment_submissions"].append(view)
    from backend.app.services import fulfillment_services, refund_services

    result["production"] = fulfillment_services.production_view(order, url, admin)
    result["drafts"] = fulfillment_services.drafts_view(order, url, admin)
    result["cancellation_requests"] = refund_services.cancellation_view(order, url, admin)
    result["refunds"] = refund_services.refunds_view(order, url, admin)
    result["customer_actions"] = customer_actions(order)
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


SUBMISSION_LABELS = {
    "pending": "Pending Verification",
    "verified": "Verified",
    "rejected": "Rejected",
    "receipt_requested": "New Receipt Requested",
}


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


def order_files(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every file stored with an order: uploads, drafts, payment proofs, production and delivery photos, refund proofs."""
    from backend.app.services import fulfillment_services, refund_services

    attachments = list(order["attachments"]) + ([order["draft_attachment"]] if order["draft_attachment"] else [])
    attachments += [item["proof"] for item in submissions(order) if item.get("proof")]
    attachments += fulfillment_services.files(order)
    attachments += refund_services.files(order)
    return [item for item in attachments if isinstance(item, dict) and item.get("id")]


def attachment_response(order: Mapping[str, Any], file_id: str, inline: bool = False) -> Response:
    attachment = next((item for item in order_files(order) if item["id"] == file_id), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="File not found.")
    return file_services.response(attachment["filename"], attachment["mime_type"], attachment["name"], inline=inline)


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
        pickup: dict[str, Any] = {"method": "pickup"}
        location = shop_settings_services.pickup_location(value.get("pickup_location_id"))
        if location is not None:
            pickup["pickup_location"] = {key: location[key] for key in ("id", "name", "address", "hours", "instructions")}
        preferred = value.get("preferred_pickup_date")
        if preferred not in (None, ""):
            preferred = _text(preferred, "Preferred pickup date", 10)
            try:
                chosen = date.fromisoformat(preferred)
            except ValueError as error:
                raise HTTPException(status_code=422, detail="Enter a valid preferred pickup date.") from error
            today = date.fromisoformat(now_iso()[:10])
            if not today <= chosen <= date(today.year + 1, today.month, min(today.day, 28)):
                raise HTTPException(status_code=422, detail="The preferred pickup date must be today or later, within a year.")
            pickup["preferred_pickup_date"] = preferred
        notes = _text(value.get("pickup_notes", "") or "", "Pickup notes", 500)
        if notes:
            pickup["pickup_notes"] = notes
        return pickup
    cleaned: dict[str, Any] = {"method": "delivery"}
    for key, label, required, limit in DELIVERY_FIELDS:
        raw = value.get(key, "")
        text = _text("" if raw is None else raw, label, limit, required)
        if required or text or key == "landmark":
            cleaned[key] = text
    try:
        cleaned["contact_number"] = validate_phone(cleaned["contact_number"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"Delivery contact number: {error}") from error
    # A municipality must belong to the selected province; canonical names keep existing orders readable.
    address = location_services.resolve_address(cleaned["province"], cleaned["city"])
    cleaned["province"] = address["province"]
    cleaned["city"] = address["city"]
    # The delivery area, fee, and courier estimate are worked out on the server from the address.
    cleaned.update(shop_settings_services.delivery_quote(cleaned["province"], value.get("area_id")))
    return cleaned


@file_services.compensating_files
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
    decoded_uploads = _decode_order_uploads(product, payload.get("files", []), payload.get("field_files"), str(customer_id))
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
    complete = isinstance(estimate, (int, float)) and estimate > 0 and not pricing.get("requires_quote")
    # A standard size from the published price list (Tarpaulin) is also a fixed price: size price x quantity.
    # A custom size has no price-list total and is quoted by the administrator.
    standard_size = pricing.get("price_basis") == "standard_size" and complete and not package
    fixed_price = (bool(package) or standard_size) and complete
    fee = float(fulfillment_choice.get("delivery_fee") or 0.0)
    if fixed_price:
        customization["fixed_price"] = True
        customization["price_basis"] = "package" if package else "standard_size"
    if standard_size:
        size = pricing.get("standard_size") or {}
        # The size and price used for this order, kept even if the price list changes later.
        customization["price_list"] = {
            "size_id": size.get("id", ""),
            "size": size.get("label", ""),
            "unit_price": size.get("unit_price"),
            "quantity": quantity,
            "computed_total": estimate,
        }
    if standard_size:
        size_text = (pricing.get("standard_size") or {}).get("label", "standard size")
        history_text = (
            f"Customer submitted an order for {quantity:,} pc(s) in the standard size {size_text}. Price-list total PHP "
            f"{estimate:,.2f} (50% down payment PHP {round(estimate * DOWN_PAYMENT_RATE, 2):,.2f}); awaiting administrator approval."
        )
    elif fixed_price:
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
    if fulfillment_choice["method"] == "delivery":
        history_text += f" Delivery area: {fulfillment_choice['area_label']}; delivery fee PHP {fee:,.2f} (calculated by the shop)."
        if fixed_price:
            history_text += f" Order total with delivery: PHP {round(float(estimate) + fee, 2):,.2f}."
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
        "total_amount": round(float(estimate) + fee, 2) if fixed_price else None,
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
            "delivery_fee": fee,
            "down_payment": round(float(order["total_amount"]) * DOWN_PAYMENT_RATE, 2) if fixed_price else None,
            "package": {
                "id": package["id"], "name": package["name"], "price": package["price"], "inclusions": package["inclusions"],
            } if package else None,
            "design": {"id": design["id"], "name": design["name"], "is_sample": design["is_sample"]} if design is not None else None,
            "price_list": customization.get("price_list"),
        },
    }
    try:
        order_repository.create_order(order, idempotency_key, audit=audit)
    except sqlite3.IntegrityError as error:
        for attachment in files:
            file_services.delete(attachment["filename"])
        existing = order_repository.get_order_by_idempotency_key(idempotency_key)
        if existing is not None:
            return _replayed_order(existing, customer_id), True
        raise HTTPException(status_code=409, detail="The order conflicted with an existing database record.") from error
    except Exception:
        for attachment in files:
            file_services.delete(attachment["filename"])
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
    order = customer_owned_order(reference, None, customer_id)
    view = order_view(order)
    view["status_timeline"] = status_timeline(order)
    return view


def status_timeline(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """When the order reached each status, from the audit log (the order tracking timeline's dates)."""
    reached = [{"status": "Pending Confirmation", "at": order["created_at"]}]
    for entry in audit_repository.list_for_order(order["reference"]):
        status = (entry.get("details") or {}).get("status_to")
        if status and status != reached[-1]["status"]:
            reached.append({"status": status, "at": entry["at"]})
    if reached[-1]["status"] != order["status"]:
        # Older orders changed before the audit log existed: the current status, from the last update.
        reached.append({"status": order["status"], "at": order["updated_at"]})
    return reached


def get_customer_order_file(customer_id: str, reference: str, file_id: str) -> Response:
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
    claim_ops: list[dict[str, Any]] | None = None,
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
            order["reference"], updates, event, expected_version=version, audit=audit,
            notifications=notifications, claim_ops=claim_ops,
        )
    except order_repository.StaleOrderError as error:
        raise HTTPException(
            status_code=409,
            detail="This order was changed by another action. Reload the order and try again.",
        ) from error
    except order_repository.ReferenceConflict as error:
        raise HTTPException(status_code=409, detail=error.detail) from error
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


def _drafts_with_response(order: Mapping[str, Any], changes: dict[str, Any]) -> list[dict[str, Any]]:
    """The draft history with ``changes`` applied to the latest draft waiting for the customer."""
    from backend.app.services import fulfillment_services

    drafts = fulfillment_services.draft_history(order)
    for draft in reversed(drafts):
        if draft.get("status") == "pending":
            draft.update(changes)
            break
    return drafts


def approve_customer_draft(reference: str, email: Any, customer_id: str | None = None) -> dict[str, Any]:
    order = customer_owned_order(reference, email, customer_id)
    if order["status"] != "Awaiting Customer Approval" or not order["draft_attachment"]:
        raise HTTPException(status_code=409, detail="There is no draft waiting for approval.")
    ensure_no_pending_cancellation(order)
    now = now_iso()
    return _save(
        order,
        {"status": "Approved for Printing", "drafts": _drafts_with_response(order, {"status": "approved", "responded_at": now})},
        "Customer approved the draft. The order may now move to production.",
        audit=_audit("draft.approved", CUSTOMER, customer_id, draft=order["draft_attachment"]["name"]),
        notifications=[notification_services.build(
            order, "draft_approved", f"draft-approved:{order['draft_attachment']['id']}", "Draft approved",
            f"You approved the draft for order {order['reference']}. The shop can now schedule printing.",
        )],
    )


def request_draft_changes(reference: str, email: Any, requested_changes: Any, customer_id: str | None = None) -> dict[str, Any]:
    note = _text(requested_changes, "Requested changes", 1000, True)
    order = customer_owned_order(reference, email, customer_id)
    if order["status"] != "Awaiting Customer Approval":
        raise HTTPException(status_code=409, detail="There is no draft waiting for review.")
    now = now_iso()
    draft_id = (order["draft_attachment"] or {}).get("id", "")
    return _save(
        order,
        {
            "status": "Draft Preparation",
            "admin_notes": note,
            "drafts": _drafts_with_response(order, {"status": "revision_requested", "responded_at": now, "revision_note": note}),
        },
        f"Customer requested draft changes: {note}",
        audit=_audit("draft.changes_requested", CUSTOMER, customer_id, note=note),
        notifications=[notification_services.build(
            order, "revision_requested", f"revision:{draft_id}", "Revision request received",
            f"The shop received your revision request for order {order['reference']}: “{note}”. A revised draft will be sent for your approval.",
        )],
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
    if quote.get("amount") is not None and abs(quotation_total(quote) - total) > 0.005:
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


def _ensure_reference_unused(
    reference_text: str,
    ignore_submission_id: str | None = None,
    order_reference: str | None = None,
) -> None:
    """A payment reference (receipt or transaction number) can be recorded only once across all orders.

    A clearer-receipt request releases the reference only on the order that requested it; another order may
    not adopt it.
    """
    key = _payment_reference_key(reference_text)
    if not key:
        return
    for other in order_repository.list_orders():
        for payment in other["payments"]:
            if _payment_reference_key(str(payment.get("reference") or "")) == key:
                raise HTTPException(status_code=409, detail="This payment reference has already been recorded. Check the reference number.")
        same_order = order_reference is not None and str(other.get("reference") or "") == str(order_reference)
        for item in submissions(other):
            superseded_here = item.get("status") == "receipt_requested" and same_order
            if item.get("id") == ignore_submission_id or item.get("status") == "rejected" or superseded_here:
                # A rejected or locally superseded receipt is history, not a live claim on this reference.
                # A clearer receipt normally keeps the same transaction reference on the same order.
                continue
            if _payment_reference_key(str(item.get("reference") or "")) == key:
                raise HTTPException(status_code=409, detail="This payment reference has already been submitted. Check the reference number.")


def admin_payment_methods() -> list[str]:
    """Methods the administrator can record: the standard list plus every configured e-wallet."""
    methods = list(PAYMENT_METHODS)
    for provider in shop_settings_services.configured_providers():
        if provider not in methods:
            methods.insert(len(methods) - 1, provider)
    return methods


def _payment_method(value: Any) -> str:
    method = _text(value, "Payment method", 60, True)
    allowed = admin_payment_methods()
    if method not in allowed:
        raise HTTPException(status_code=422, detail=f"Choose a payment method: {', '.join(allowed)}.")
    return method


def _scan_receipt(
    order: Mapping[str, Any],
    submission: Mapping[str, Any],
    metadata: Mapping[str, Any],
    contents: bytes,
    method: str,
    due: Mapping[str, Any],
    orders: list[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run OCR and matching for one receipt. Never raises because of OCR; returns structured feedback."""
    config = shop_settings_services.verification_settings(method)
    ocr = ocr_services.extract_receipt(contents, str(metadata.get("mime_type") or ""), name=str(metadata.get("name") or ""))
    ocr_view = ocr.view()
    verification = verification_services.build_verification(
        order,
        submission,
        ocr_view,
        method=method,
        required_amount=float(due["amount"]),
        customer_name=str((order.get("customer") or {}).get("name") or ""),
        recipient_expected=str(config.get("account_name") or ""),
        ocr_reference=str(ocr_view["fields"].get("reference") or ""),
        verification_method=str(config.get("verification_method") or "manual"),
        gateway_config=config.get("gateway_config", {}),
        orders=orders if orders is not None else order_repository.list_orders(),
        now=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    return ocr_view, verification


def scan_customer_payment(reference: str, email: Any, payload: Mapping[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """Read-only receipt preview: OCR and matching feedback before the customer submits.

    Nothing is saved, no file is stored, and no notification is sent. The authoritative scan runs again on
    submission, so a stale preview can never be used to claim a payment was verified.
    """
    order = customer_owned_order(reference, email, customer_id)
    if pending_submissions(order):
        raise HTTPException(status_code=409, detail="A payment is already waiting for the shop to verify it.")
    due = next_customer_payment(order)
    if due is None:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    method = shop_settings_services.default_provider() if payload.get("method") in (None, "") else shop_settings_services.active_provider(payload.get("method"))
    if payload.get("proof") in (None, ""):
        raise HTTPException(status_code=422, detail=f"Upload the {method} payment screenshot or receipt.")
    metadata, contents = _decode_receipt(payload.get("proof"))
    reference_text = _text(payload.get("reference", "") or "", "Payment reference", 120)
    submission = {"id": "preview", "reference": reference_text, "amount": due["amount"], "receipt_hash": ocr_services.receipt_hash(contents)}
    ocr_view, verification = _scan_receipt(order, submission, metadata, contents, method, due)
    return {
        "reference": order["reference"],
        "method": method,
        "required_amount": due["amount"],
        "stage": due["stage"],
        "label": due["label"],
        "ocr": ocr_view,
        "verification": verification,
        "notice": "Scanning reads the receipt only. Payment is confirmed after the shop or provider verifies it.",
    }


@file_services.compensating_files
def submit_customer_payment(reference: str, email: Any, payload: Mapping[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """The customer submits a receipt for the server-calculated 50% down payment or remaining balance.

    OCR and matching annotate the submission, but only an independent provider confirmation or an explicit
    administrator action can turn it into a payment record. Only one submission can wait for verification at
    a time; older submissions stay in the order's history.
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
    ensure_no_pending_cancellation(order)
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
    # Only an active e-wallet configured by the shop (GCash when an older page does not name one).
    method = shop_settings_services.default_provider() if method in (None, "") else shop_settings_services.active_provider(method)
    reference_text = _text(payload.get("reference"), f"{method} reference number", 120, True)
    if len(_payment_reference_key(reference_text)) < 4:
        raise HTTPException(status_code=422, detail=f"Enter the complete {method} reference or transaction number.")
    if payload.get("proof") in (None, ""):
        raise HTTPException(status_code=422, detail=f"Upload the {method} payment screenshot or receipt.")
    _ensure_reference_unused(reference_text, order_reference=reference)
    proof, contents = _decode_receipt(payload.get("proof"))
    submission = {
        "id": secrets.token_hex(8),
        "amount": due["amount"],
        "stage": due["stage"],
        "method": method,
        "reference": reference_text,
        "proof": proof,
        "receipt_hash": ocr_services.receipt_hash(contents),
        "submitted_at": now_iso(),
        "status": "pending",
        "admin_notes": [],
        "receipt_requests": [],
    }
    ocr_view, verification = _scan_receipt(order, submission, proof, contents, method, due)
    submission["ocr"] = ocr_view
    submission["verification"] = verification
    submission["verification_status"] = verification["status"]
    submission["verification_method"] = verification["verification_method"]
    try:
        file_services.save(proof, contents)  # type: ignore[arg-type]
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(status_code=500, detail="The proof of payment could not be saved.") from error
    notifications = [notification_services.build(
        order, "payment_receipt_uploaded", f"payment-uploaded:{submission['id']}", "Payment receipt received",
        f"We received your {method} receipt of PHP {due['amount']:,.2f} for {due['label'].lower()} "
        f"(reference {reference_text}). It is being processed; no payment is counted until it is verified.",
    )]
    status = verification["status"]
    if status in {verification_services.ADMIN_REVIEW, verification_services.DUPLICATE}:
        notifications.append(notification_services.build(
            order, "payment_additional_verification", f"payment-review:{submission['id']}", "Payment needs additional verification",
            f"Your {method} receipt (reference {reference_text}) needs additional verification. "
            "The shop will compare it with its records; you do not need to pay again yet.",
        ))
    elif status == verification_services.PENDING:
        notifications.append(notification_services.build(
            order, "payment_processing", f"payment-processing:{submission['id']}", "Payment is being processed",
            f"Your {method} receipt (reference {reference_text}) was read successfully and is waiting for confirmation.",
        ))
    try:
        result = _save(
            order,
            {"payment_submissions": submissions(order) + [submission]},
            f"Customer submitted a payment of PHP {due['amount']:,.2f} ({due['label'].lower()}) via {method} "
            f"(reference {reference_text}) with a screenshot/receipt. OCR classification: {verification['status_label']} "
            f"(confidence {verification['confidence']:.0%}); this is not proof that funds were received.",
            audit=_audit(
                "payment.submitted", CUSTOMER, customer_id,
                submission_id=submission["id"], amount=due["amount"], stage=due["stage"], method=method, reference=reference_text,
                ocr_status=status, ocr_confidence=verification["confidence"], verification_method=verification["verification_method"],
            ),
            notifications=notifications,
            claim_ops=[{
                "op": "acquire", "kind": "payment", "key": _payment_reference_key(reference_text),
                "context": submission["id"], "detail": "This payment reference has already been submitted. Check the reference number.",
            }],
        )
    except HTTPException:
        file_services.delete(proof["filename"])
        raise
    if verification["auto_confirm"]:
        # Only an independent provider confirmation can reach this branch. The existing verification service
        # re-checks the balance, reference, and one-payment-per-submission rule before recording money.
        try:
            verify_payment_submission(
                reference, submission["id"],
                {"received": True, "amount": due["amount"]},
                actor=verification["provider"].get("provider") or "payment provider",
            )
        except HTTPException as error:
            logger.warning("Provider-confirmed payment could not be recorded for %s: %s", reference, error.detail)
        else:
            # The verification service returns the admin view. Reload so the customer gets the same
            # response shape (and the same file URLs) as every other customer payment submission.
            return order_after_update(order_repository.get_order(reference), admin=False)
    return result


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


def get_admin_order_file(reference: str, file_id: str) -> Response:
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
        raise HTTPException(
            status_code=409,
            detail="This order uses a published price (package or standard size price list). Approve the order instead of quoting it.",
        )
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
    # The quotation covers the printing work; the delivery fee calculated at checkout is added on top.
    subtotal = round(float(amount), 2)
    fee = delivery_fee(order)
    total = round(subtotal + fee, 2)
    down = round(total * DOWN_PAYMENT_RATE, 2)
    previous = quotation(order)
    previous_amount = quotation_total(previous) if previous.get("amount") is not None else None
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
        "subtotal": subtotal,
        "delivery_fee": fee,
        "previous_amount": previous_amount,
        "note": note,
        "sent_at": now,
        "sent_by": actor,
        "status": "sent",
        "responded_at": None,
        "decline_reason": "",
    })
    quote = {
        "amount": subtotal,
        "delivery_fee": fee,
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
    if fee:
        event += f" Printing PHP {subtotal:,.2f} + delivery fee PHP {fee:,.2f}."
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
        raise HTTPException(status_code=409, detail="Only fixed-price orders (packages and standard sizes) are approved this way. Send a quotation instead.")
    if order["status"] not in {"Pending Confirmation", "Under Review"}:
        raise HTTPException(status_code=409, detail="This order has already been approved or closed.")
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    total = float(order["total_amount"] or 0)
    down = down_payment_due(order)
    switched = " The order was placed with Cash on Delivery, which is no longer offered, so it now uses the 50% down payment." if is_cod(order) else ""
    shared = "provided" if instructions else "not included"
    customization = order.get("customization") or {}
    package = customization.get("package") or {}
    price_list = customization.get("price_list") or {}
    if price_basis(order) == "standard_size":
        kind_text = "standard-size order"
        title = "Order approved"
        what = f"{(order.get('product') or {}).get('name', 'order')} ({price_list.get('size') or 'standard size'})"
    else:
        kind_text = "package order"
        title = "Package order approved"
        what = f"{package.get('name') or 'package'}"
    return _save(
        order,
        {"status": "Awaiting Down Payment", "payment_instructions": instructions, "payment_option": NEW_ORDER_PAYMENT_OPTION},
        f"Administrator approved the {kind_text} at PHP {total:,.2f}. A 50% down payment of PHP {down:,.2f} is required "
        f"to confirm the order.{switched} Payment instructions were {shared}.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "order.approved", ADMIN, actor, total=total, down_payment=down,
            package=package.get("name", ""), standard_size=price_list.get("size", ""),
        ),
        notifications=[notification_services.build(
            order, "down_payment_requested", "down-payment-requested", title,
            f"The shop approved your {what} order (PHP {total:,.2f}). Pay the 50% down payment of "
            f"PHP {down:,.2f} through GCash and upload the screenshot or receipt in your customer portal to confirm your order.",
        )],
    )


# ------------------------------------------------------------------ payments


def _payment_kind(order: Mapping[str, Any], already_paid: float) -> str:
    if is_cod(order):
        return "COD collection"
    return "Down payment" if already_paid < down_payment_due(order) - 0.005 else "Balance payment"


def _payment_updates(order: OrderRecord, payment: dict[str, Any]) -> tuple[OrderUpdates, bool, list[dict[str, Any]]]:
    """The payment list with ``payment`` added, whether it completes the down payment (confirming the order),
    and the extra customer notifications.

    Only verified money is passed here (a verified customer submission or money the administrator received),
    so an uploaded screenshot can never confirm an order by itself. A packed order waiting for its balance
    (Awaiting Full Payment) is released for pickup or dispatch as soon as it is fully paid.
    """
    payments = order["payments"] + [payment]
    updates: OrderUpdates = {"payments": payments}
    total_paid = round(sum(float(item["amount"]) for item in payments), 2)
    confirmed = order["status"] == "Awaiting Down Payment" and total_paid >= down_payment_due(order) - 0.005
    extra: list[dict[str, Any]] = []
    if confirmed:
        updates["status"] = "Confirmed"
    elif order["status"] == "Awaiting Full Payment" and total_paid >= round(float(order["total_amount"] or 0), 2) - 0.005:
        status, record, notice = release_updates(order)
        updates["status"] = status
        updates["fulfillment"] = record
        extra.append(notice)
    return updates, confirmed, extra


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
    reference_text, reference_type = _payment_reference(method if method in admin_payment_methods() else "Other", payload.get("reference"))
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
        claim_ops=[{
            "op": "acquire", "kind": "payment", "key": _payment_reference_key(reference_text),
            "detail": "This payment reference has already been recorded. Check the reference number.",
        }],
    )


def record_payment(reference: str, payload: dict[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Record money the shop received directly (cash at the shop, a transfer seen in the account)."""
    order = _admin_order(reference)
    balance = _check_payable(order)
    ensure_no_pending_cancellation(order)
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
    updates, confirmed, extra = _payment_updates(order, payment)
    return _save(
        order,
        updates,
        f"Administrator recorded PHP {payment['amount']:,.2f} received via {payment['method']} "
        f"({REFERENCE_TYPE_LABELS[reference_type].lower()} {reference_text}; {payment['kind'].lower()})."
        + (f" Note: {note}" if note else "")
        + (CONFIRMED_SUFFIX if confirmed else "")
        + (f" The order is fully paid and now {updates['status']}." if extra else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.recorded", ADMIN, actor,
            payment_id=payment["id"], amount=amount, method=method, reference=reference_text, reference_type=reference_type,
            kind=payment["kind"], confirmed=confirmed, note=note,
        ),
        notifications=[_payment_notification(order, payment, confirmed), *extra],
        claim_ops=[{
            "op": "acquire", "kind": "payment", "key": _payment_reference_key(reference_text),
            "detail": "This payment reference has already been recorded. Check the reference number.",
        }],
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
    _ensure_reference_unused(
        str(submission.get("reference") or ""),
        ignore_submission_id=submission_id,
        order_reference=str(order.get("reference") or ""),
    )
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
            "ocr_status": (submission.get("verification") or {}).get("status", "") if isinstance(submission.get("verification"), dict) else "",
            "ocr_confidence": (submission.get("ocr") or {}).get("confidence", 0) if isinstance(submission.get("ocr"), dict) else 0,
            "receipt_hash": submission.get("receipt_hash", ""),
        },
        # Historical audit data: the extracted receipt information is kept with the payment record.
        "ocr": submission.get("ocr", {}),
        "receipt_hash": submission.get("receipt_hash", ""),
    }
    stored = submission.get("verification") if isinstance(submission.get("verification"), dict) else {}
    stored.update({
        "status": verification_services.VERIFIED,
        "status_label": verification_services.STATUS_LABELS[verification_services.VERIFIED],
        "verification_method": "manual",
        "reviewed_at": now,
        "reviewed_by": actor,
        "requires_admin": False,
        "auto_confirm": False,
    })
    submission.update({
        "status": "verified", "reviewed_at": now, "reviewed_by": actor, "payment_id": payment["id"], "verified_amount": amount,
        "verification_status": verification_services.VERIFIED, "verification": stored,
    })
    updates, confirmed, extra = _payment_updates(order, payment)
    updates["payment_submissions"] = items
    reported = float(submission["amount"])
    adjusted = f" (customer reported PHP {reported:,.2f})" if abs(amount - reported) > 0.005 else ""
    verifier = "an administrator" if actor == ADMIN else str(actor or "the payment provider")
    return _save(
        order,
        updates,
        f"The customer's payment of PHP {amount:,.2f}{adjusted} via {payment['method']} "
        f"(reference {payment['reference']}) was verified by {verifier} and recorded as {payment['kind'].lower()}."
        + (CONFIRMED_SUFFIX if confirmed else "")
        + (f" The order is fully paid and now {updates['status']}." if extra else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.verified", ADMIN, actor,
            submission_id=submission_id, payment_id=payment["id"], amount=amount, reported_amount=reported,
            reference=payment["reference"], kind=payment["kind"], confirmed=confirmed,
        ),
        notifications=[_payment_notification(order, payment, confirmed), *extra],
    )


def reject_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    items, submission = _find_submission(order, submission_id)
    note = _text(payload.get("note", "") or "", "Reason", 500, True)
    stored = submission.get("verification") if isinstance(submission.get("verification"), dict) else {}
    stored.update({
        "status": verification_services.REJECTED,
        "status_label": verification_services.STATUS_LABELS[verification_services.REJECTED],
        "reviewed_at": now_iso(),
        "reviewed_by": actor,
        "rejection_reason": note,
        "requires_admin": False,
    })
    submission.update({
        "status": "rejected", "reviewed_at": stored["reviewed_at"], "reviewed_by": actor, "review_note": note,
        "verification_status": verification_services.REJECTED, "verification": stored,
    })
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
        claim_ops=[{
            "op": "release", "kind": "payment",
            "key": _payment_reference_key(str(submission.get("reference") or "")),
        }],
    )


def _submission_for_admin(reference: str, submission_id: str) -> tuple[OrderRecord, list[dict[str, Any]], dict[str, Any]]:
    order = _admin_order(reference)
    items = [dict(item) for item in submissions(order)]
    found = next((item for item in items if item.get("id") == submission_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Payment submission not found.")
    return order, items, found


def add_payment_verification_note(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Append a verification note to a submission's history without changing whether it counts as paid."""
    order, items, found = _submission_for_admin(reference, submission_id)
    note = _text(payload.get("note", "") or "", "Verification note", 500, True)
    now = now_iso()
    found.setdefault("admin_notes", []).append({"note": note, "at": now, "by": actor})
    stored = found.get("verification") if isinstance(found.get("verification"), dict) else {}
    # Keep this text customer-safe: the admin record already stores who wrote it in admin_notes.
    stored["notes"] = [*stored.get("notes", []), note]
    found["verification"] = stored
    amount = float(found.get("amount") or 0)
    return _save(
        order,
        {"payment_submissions": items},
        f"Administrator added a verification note to the {found.get('method', '')} payment submission "
        f"(reference {found.get('reference', '')}, PHP {amount:,.2f}): {note}",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("payment.verification_note", ADMIN, actor, submission_id=submission_id, note=note, reference=found.get("reference", "")),
        notifications=[notification_services.build(
            order, "payment_verification_note", f"payment-note:{submission_id}:{len(found['admin_notes'])}",
            "Update on your payment review",
            f"The shop added a note to your payment review for reference {found.get('reference', '')}: {note}",
        )],
    )


def request_clearer_receipt(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Ask the customer to submit a clearer receipt. The old submission stays in history and is not paid."""
    order, items, found = _submission_for_admin(reference, submission_id)
    if found.get("status") != "pending":
        raise HTTPException(status_code=409, detail="Only a pending receipt can be replaced.")
    note = _text(payload.get("note", "") or "Please upload a clearer receipt showing the reference, amount, and recipient.", "Note", 500)
    now = now_iso()
    found.setdefault("receipt_requests", []).append({"at": now, "by": actor, "note": note})
    # This frees the single pending slot so the customer can upload again; every earlier receipt is kept.
    found["status"] = "receipt_requested"
    stored = found.get("verification") if isinstance(found.get("verification"), dict) else {}
    stored.update({"status": verification_services.ADMIN_REVIEW, "status_label": verification_services.STATUS_LABELS[verification_services.ADMIN_REVIEW],
                   "requested_receipt_at": now, "requested_receipt_by": actor})
    found["verification"] = stored
    amount = float(found.get("amount") or 0)
    return _save(
        order,
        {"payment_submissions": items},
        f"Administrator requested a clearer receipt for the {found.get('method', '')} payment submission "
        f"(reference {found.get('reference', '')}, PHP {amount:,.2f}). The earlier receipt stays in the audit history.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("payment.receipt_requested", ADMIN, actor, submission_id=submission_id, note=note, reference=found.get("reference", "")),
        notifications=[notification_services.build(
            order, "payment_receipt_requested", f"payment-receipt-requested:{submission_id}",
            "Please upload a clearer payment receipt",
            f"We could not fully read your receipt for reference {found.get('reference', '')}. {note} "
            "Open your customer portal to upload a new receipt. No payment has been recorded yet.",
        )],
        claim_ops=[{
            "op": "release", "kind": "payment",
            "key": _payment_reference_key(str(found.get("reference") or "")),
        }],
    )


# ------------------------------------------------------------------ drafts, payment option, status


@file_services.compensating_files
def upload_draft(reference: str, payload: dict[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Send a draft (or a revised draft) for the customer's approval. Earlier drafts are kept in the history."""
    from backend.app.services import fulfillment_services

    order = _admin_order(reference)
    ensure_no_pending_cancellation(order)
    if order["status"] not in DRAFTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="A draft can only be sent after the order is confirmed.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before sending a draft.")
    if not down_payment_met(order):
        raise HTTPException(status_code=409, detail="Verify the required 50% down payment before sending a draft.")
    remarks = _text(payload.get("remarks", "") or "", "Design remarks", 1000)
    metadata, contents = _decode_upload(payload.get("file"))
    try:
        file_services.save(metadata, contents)  # type: ignore[arg-type]
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(status_code=500, detail="The draft file could not be saved.") from error
    kind = "revised draft" if order["draft_attachment"] else "draft"
    drafts = fulfillment_services.draft_history(order)
    for earlier in drafts:
        if earlier.get("status") == "pending":
            earlier.update({"status": "superseded", "superseded_at": now_iso()})
    drafts.append({
        "id": metadata["id"], "file": metadata, "remarks": remarks, "sent_at": now_iso(), "sent_by": actor,
        "status": "pending", "responded_at": None, "revision_note": "", "number": len(drafts) + 1,
    })
    try:
        return _save(
            order,
            {"draft_attachment": metadata, "status": "Awaiting Customer Approval", "drafts": drafts},
            f"Administrator attached {kind} '{metadata['name']}' (draft {len(drafts)}) for customer review. Printing is not approved."
            + (f" Remarks: {remarks}" if remarks else ""),
            payload.get("expected_version"),
            admin=True,
            audit=_audit("draft.sent", ADMIN, actor, draft=metadata["name"], revised=kind == "revised draft"),
            notifications=[notification_services.build(
                order, "draft_ready", f"draft:{metadata['id']}", "Draft ready for your approval",
                f"The shop sent a {kind} for your order. Review it in your customer portal and approve it or request changes.",
            )],
        )
    except HTTPException:
        file_services.delete(metadata["filename"])
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
    "In Production": {"Quality Checking"},
    # Quality Checking → Packaging (quality check passed), Packaging → Awaiting Full Payment / Ready for
    # Pickup / Ready for Dispatch (packaging completed), Ready for Dispatch → Out for Delivery (dispatch),
    # Out for Delivery → Delivered / Delivery Failed, Ready for Pickup → Order Claimed (claimant verified)
    # happen through their own actions in fulfillment_services, which record the details they need.
    "Delivered": {"Completed"},
    "Order Claimed": {"Completed"},
}
# Orders released before Quality Checking and Packaging existed may still be completed directly.
LEGACY_RELEASE_TRANSITIONS = {"Ready for Pickup": {"Completed"}, "Out for Delivery": {"Completed"}}
# Status changes the customer is told about.
STATUS_NOTIFICATIONS = {
    "In Production": ("Production started", "Printing of your order has started."),
    "Ready for Pickup": ("Ready for pickup", "Your order is ready for pickup at the shop."),
    "Out for Delivery": ("Out for delivery", "Your order is out for delivery."),
    "Completed": ("Order completed", "Your order is completed. Thank you for ordering with ImPRESSion PrintShop!"),
    "Rejected": ("Order request rejected", "The shop could not accept your order request."),
    "Cancelled": ("Order cancelled", "The shop cancelled your order."),
}


def allowed_transitions(order: Mapping[str, Any]) -> list[str]:
    """The direct status moves that fit this order (its pricing and its delivery or pickup choice)."""
    allowed = set(ADMIN_STATUS_TRANSITIONS.get(order["status"], set()))
    if legacy_release(order):
        allowed |= LEGACY_RELEASE_TRANSITIONS[order["status"]]
    if is_fixed_price(order):
        allowed.discard("Awaiting Quotation")
    if pending_cancellation(order):
        allowed &= CANCELLED_STATUSES
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
        if pending_cancellation(order):
            ensure_no_pending_cancellation(order)
        if order["status"] == "In Production" and next_status in RELEASE_STATUSES | {"Packaging"}:
            raise HTTPException(
                status_code=409,
                detail="Printed orders go through Quality Checking and Packaging before they are released for pickup or delivery.",
            )
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
    if next_status in CANCELLED_STATUSES and not note:
        raise HTTPException(status_code=422, detail="Enter a reason before rejecting or cancelling this order.")
    updates: OrderUpdates = {"status": next_status}
    event = f"Administrator updated the order status to {next_status}."
    if next_status in {"In Production", "Quality Checking"}:
        record = production(order)
        now = now_iso()
        if next_status == "In Production":
            record.setdefault("started_at", now)
            record.setdefault("ordered_quantity", (order.get("customization") or {}).get("quantity"))
        else:
            record["quality_started_at"] = now
        updates["production"] = record
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


def quotation_total(quote: Mapping[str, Any]) -> float:
    """What the customer pays for a quotation: the quoted printing amount plus the delivery fee."""
    if quote.get("amount") is None:
        return 0.0
    return round(float(quote["amount"]) + float(quote.get("delivery_fee") or 0.0), 2)


def refunded_amount(order: Mapping[str, Any]) -> float:
    """Money actually returned to the customer (completed refunds only)."""
    refunds = order.get("refunds") if isinstance(order.get("refunds"), list) else []
    return round(sum(float(item.get("amount") or 0) for item in refunds if item.get("status") == "refunded"), 2)


def customer_actions(order: Mapping[str, Any]) -> dict[str, bool]:
    """The customer portal buttons that are valid for this order right now (the server enforces the same rules)."""
    from backend.app.services import fulfillment_services, refund_services

    status = order["status"]
    quote = quotation(order)
    paid = bool(order["payments"])
    return {
        "accept_quotation": status == "Quotation Sent" and quote.get("status") == "sent",
        "decline_quotation": status == "Quotation Sent" and quote.get("status") == "sent",
        "pay": next_customer_payment(order) is not None and not pending_submissions(order) and not pending_cancellation(order),
        "approve_draft": status == "Awaiting Customer Approval" and bool(order["draft_attachment"]),
        "request_revision": status == "Awaiting Customer Approval" and bool(order["draft_attachment"]),
        "cancel": status in PENDING_STATUSES and not paid and not pending_submissions(order),
        "request_cancellation": refund_services.can_request_cancellation(order),
        "request_refund": refund_services.can_request_refund(order),
        "report_delivery_issue": fulfillment_services.can_report_issue(order),
        "download_receipt": paid,
    }


REPRINTABLE_STATUSES = {"Delivered", "Order Claimed", "Completed"}


def active_reprint(order: Mapping[str, Any]) -> dict[str, Any] | None:
    reprint = production(order).get("reprint")
    return reprint if isinstance(reprint, dict) and reprint.get("status") == "in_progress" else None


def counts_as_completed(order: Mapping[str, Any]) -> bool:
    """Completed, or completed before a reprint that is still in progress (reports keep the original completion)."""
    if order["status"] == "Completed":
        return True
    reprint = active_reprint(order)
    return bool(reprint and reprint.get("original_status") == "Completed" and order.get("completed_at"))


def revenue_basis(order: Mapping[str, Any]) -> float | None:
    """The total recognized as revenue: the original total while a reprint of a completed order is in progress."""
    if order["status"] == "Completed":
        return order.get("total_amount")
    reprint = active_reprint(order)
    if reprint and counts_as_completed(order):
        return reprint.get("original_total")
    return None

