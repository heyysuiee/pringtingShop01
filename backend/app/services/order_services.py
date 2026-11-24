from __future__ import annotations

import logging
import math
import re
import secrets
import sqlite3
from collections.abc import Iterable, Mapping
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
    # Down-payment/work-approval workflow. These sit alongside the older draft and production stages so
    # existing orders keep their familiar statuses while new orders can follow the review-first flow.
    "In Progress",
    "Ready for Customer Review",
    "Revision Requested",
    "Approved by Customer",
    "Awaiting Final Payment",
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
# Order progress (work) and payment progress are deliberately separate. A partially paid order can be In Progress.
WORK_REVIEW_STATUSES = {"Ready for Customer Review", "Revision Requested", "Approved by Customer", "Awaiting Final Payment"}
# Active orders: confirmed by a verified down payment and not finished yet.
PROCESSING_STATUSES = (
    {"Confirmed", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "Awaiting Full Payment"}
    | PRODUCTION_STATUSES
    | RELEASE_STATUSES
    | {"In Progress"}
    | WORK_REVIEW_STATUSES
)
DRAFTABLE_STATUSES = {"Confirmed", "Draft Preparation", "Awaiting Customer Approval"}
CANCELLED_STATUSES = {"Cancelled", "Rejected"}
# Payments can be recorded only after the price is final and the customer has chosen how to pay (a customized
# quotation must be accepted; a package order must be confirmed). Completed is included only so a balance left
# on an order completed before full payment was required can still be recorded (the balance check prevents overpaying).
PAYABLE_STATUSES = {"Awaiting Down Payment", "Completed"} | PROCESSING_STATUSES
# Customers may report a payment (reference + proof) while money is still due on a down-payment order.
CUSTOMER_PAYABLE_STATUSES = {"Awaiting Down Payment"} | PROCESSING_STATUSES
# Payment status is never derived from the production stage: it comes only from the confirmed total, the
# required down payment, and the cumulative amount the shop has actually received and confirmed.
# A 50% payment is a minimum, not a ceiling, so every amount above it stays credited to the order.
MONEY_TOLERANCE = 0.005
NO_PAYMENT = "No Payment"
INSUFFICIENT_DOWN_PAYMENT = "Insufficient Down Payment"
DOWN_PAYMENT_PAID = "Down Payment Paid"
PARTIALLY_PAID = "Partially Paid"
FULLY_PAID = "Fully Paid"
EXCESS_PAYMENT = "Excess Payment"
PAYMENT_STATUSES = (
    NO_PAYMENT,
    INSUFFICIENT_DOWN_PAYMENT,
    DOWN_PAYMENT_PAID,
    PARTIALLY_PAID,
    FULLY_PAID,
    EXCESS_PAYMENT,
)

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
    """Where a packed, fully paid order goes next: Ready for Dispatch (delivery) or Ready for Pickup.

    This is a release *destination*, not the order's current status. Callers that display progress must use
    ``fulfillment_status``; this value is only shown as a forward-looking release step once packing is done.
    """
    return "Ready for Dispatch" if fulfillment_method(order) == "delivery" else "Ready for Pickup"


def next_release_status(order: Mapping[str, Any]) -> str:
    """Explicit name for the release destination kept alongside the legacy ``release_status`` field."""
    return release_status(order)


def fulfillment_status(order: Mapping[str, Any]) -> str:
    """One current fulfillment label, derived from the authoritative order status.

    Payment and work progress stay in their own dimensions; this only describes what has happened to the
    physical order. It never reports a release destination before the order is actually ready.
    """
    status = order["status"]
    if status in CANCELLED_STATUSES:
        return "Not applicable"
    if status == "Ready for Pickup":
        return "Ready for pickup"
    if status == "Ready for Dispatch":
        return "Ready for dispatch"
    if status == "Out for Delivery":
        return "Out for delivery"
    if status == "Delivery Failed":
        return "Delivery failed"
    if status == "Delivered":
        return "Delivered"
    if status == "Order Claimed":
        return "Order claimed"
    if status == "Completed":
        return "Completed"
    if status == "Awaiting Full Payment":
        return "Awaiting full payment"
    if status == "Packaging":
        return "Packaging"
    if status == "Quality Checking":
        return "Quality checking"
    if status in PRODUCTION_STATUSES | {"Approved for Printing"}:
        return "In production"
    return "Not started"


def release_ready(order: Mapping[str, Any]) -> bool:
    """Whether an order may be announced as ready for pickup or dispatch right now.

    The same rule is used by packaging completion and by final-payment verification, so no order can enter a
    Ready status while money still needs review, a balance is outstanding, or a cancellation is pending.
    """
    return bool(
        not pending_cancellation(order)
        and pending_excess_amount(order) <= MONEY_TOLERANCE
        and is_fully_paid(order)
    )


def ensure_release_ready(order: Mapping[str, Any], what: str = "release this order") -> None:
    """Raise the same clear gate errors used by dispatch and pickup before any Ready status is saved."""
    ensure_no_pending_cancellation(order)
    pending_excess = pending_excess_amount(order)
    if pending_excess > MONEY_TOLERANCE:
        raise HTTPException(
            status_code=409,
            detail=f"Resolve the flagged excess of PHP {pending_excess:,.2f} before {what}. Unreviewed money is never treated as paid.",
        )
    if not is_fully_paid(order):
        breakdown = payment_breakdown(order)
        raise HTTPException(
            status_code=409,
            detail=f"Verify the remaining balance of PHP {float(breakdown['balance'] or 0):,.2f} before {what}. Unpaid orders are never released.",
        )


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


def shipping_snapshot(order: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable shipping facts saved with a delivery order.

    New orders write a nested ``shipping`` record at checkout. Older delivery orders fall back to the
    top-level fulfillment fields saved by earlier versions, so their historical fee is never recalculated.
    """
    stored = fulfillment(order)
    if fulfillment_method(order) != "delivery":
        return {}
    value = stored.get("shipping")
    if isinstance(value, dict):
        return dict(value)
    region = str(stored.get("region") or shop_settings_services.region_of(str(stored.get("province") or "")))
    label = shop_settings_services.REGIONS.get(region, region.capitalize())
    return {
        "province": str(stored.get("province") or ""),
        "city": str(stored.get("city") or ""),
        "region": region,
        "region_label": label,
        "destination_region": label,
        "delivery_fee": stored.get("delivery_fee", 0.0),
        "estimate_min_days": stored.get("estimate_min_days"),
        "estimate_max_days": stored.get("estimate_max_days"),
        "estimate_text": str(stored.get("estimate_text") or ""),
        "area_id": str(stored.get("area_id") or ""),
        "area_label": str(stored.get("area_label") or ""),
        "snapshot_recorded": False,
    }


def shipping_view(order: Mapping[str, Any]) -> dict[str, Any]:
    """Customer/admin-facing shipping snapshot, with the exact display names used across the app."""
    stored = fulfillment(order)
    if fulfillment_method(order) != "delivery":
        return {
            "delivery_province": "",
            "destination_region": "",
            "province": "",
            "city": "",
            "region": "",
            "region_label": "",
            "shipping_fee": 0.0,
            "delivery_fee": 0.0,
            "estimate_min_days": None,
            "estimate_max_days": None,
            "estimated_delivery": "",
            "estimate_text": "",
            "area_id": "",
            "area_label": "",
            "snapshot_recorded": False,
        }
    snapshot = shipping_snapshot(order)
    raw_fee = snapshot.get("delivery_fee", stored.get("delivery_fee", 0.0))
    fee = round(float(raw_fee), 2) if isinstance(raw_fee, (int, float)) and not isinstance(raw_fee, bool) else 0.0
    region = str(snapshot.get("region") or "")
    label = str(snapshot.get("region_label") or shop_settings_services.REGIONS.get(region, region.capitalize()))
    estimate = str(snapshot.get("estimate_text") or "")
    return {
        "delivery_province": str(snapshot.get("province") or ""),
        "destination_region": label,
        "province": str(snapshot.get("province") or ""),
        "city": str(snapshot.get("city") or ""),
        "region": region,
        "region_label": label,
        "shipping_fee": fee,
        "delivery_fee": fee,
        "estimate_min_days": snapshot.get("estimate_min_days"),
        "estimate_max_days": snapshot.get("estimate_max_days"),
        "estimated_delivery": estimate,
        "estimate_text": estimate,
        "area_id": str(snapshot.get("area_id") or ""),
        "area_label": str(snapshot.get("area_label") or ""),
        "calculated_at": snapshot.get("calculated_at"),
        "snapshot_recorded": bool(snapshot.get("snapshot_recorded", True)),
    }


def delivery_fee(order: Mapping[str, Any]) -> float:
    """The delivery fee snapshotted by the server at checkout (0 for pickup and older orders)."""
    return float(shipping_view(order)["shipping_fee"])


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
    ensure_release_ready(order, "release the order")
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


def validate_down_payment(value: Any, total: float) -> float | None:
    """An administrator-set down payment: a positive amount no larger than the confirmed total.

    Omitting it keeps the original 50% rule. The server validates the money; the request body never decides
    what counts as paid on its own.
    """
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(status_code=422, detail="Enter a valid down payment amount.")
    amount = round(float(value), 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="The down payment must be greater than zero.")
    if amount > round(float(total), 2) + 0.005:
        raise HTTPException(status_code=422, detail="The down payment cannot exceed the total order amount.")
    return amount


def required_down_payment(order: Mapping[str, Any]) -> float:
    """The administrator-set down payment, or the original 50% rule for older/unset orders.

    A stored value is authoritative only while it is a positive finite amount no larger than the confirmed
    total. This keeps a hand-edited or partial record from ever creating a negative remaining balance.
    """
    total = order.get("total_amount")
    if total is None:
        return 0.0
    stored = order.get("required_down_payment")
    if _valid_amount(stored) and round(float(stored), 2) <= round(float(total), 2):
        return round(float(stored), 2)
    return round(float(total) * DOWN_PAYMENT_RATE, 2)


def down_payment_due(order: Mapping[str, Any]) -> float:
    return required_down_payment(order)


def down_payment_met(order: Mapping[str, Any]) -> bool:
    """COD needs no down payment; otherwise verified payments must cover the required down payment."""
    if order.get("total_amount") is None or not payment_option(order):
        return False
    return is_cod(order) or paid_amount(order) >= down_payment_due(order) - 0.005


def payment_status_from_amounts(
    total: Any,
    required_down_payment: Any,
    confirmed_payments: Any,
    *,
    cod: bool = False,
    excess: Any = 0.0,
) -> str:
    """Classify confirmed money, never a customer's claim or an OCR reading.

    ``confirmed_payments`` is the cumulative total of credited payment records. Every payment is added to it;
    a later payment never replaces an earlier one. The 50% down payment is only the minimum initial payment,
    so an amount above it is credited in full and simply changes the status to Partially Paid or Fully Paid.
    Money above the order total is held separately as ``excess`` and keeps the status at Excess Payment until
    an administrator resolves it; it is never silently counted as paid.
    """
    if total is None:
        return NO_PAYMENT
    total_value = round(float(total), 2)
    paid = round(max(0.0, float(confirmed_payments or 0.0)), 2)
    required = 0.0 if cod else round(
        min(max(0.0, float(required_down_payment or 0.0)), total_value), 2
    )
    if round(float(excess or 0.0), 2) > MONEY_TOLERANCE:
        return EXCESS_PAYMENT
    if paid > total_value + MONEY_TOLERANCE:
        return EXCESS_PAYMENT
    if paid >= total_value - MONEY_TOLERANCE:
        return FULLY_PAID
    if paid <= MONEY_TOLERANCE:
        return NO_PAYMENT
    if required > 0:
        if paid < required - MONEY_TOLERANCE:
            return INSUFFICIENT_DOWN_PAYMENT
        if paid <= required + MONEY_TOLERANCE:
            return DOWN_PAYMENT_PAID
    return PARTIALLY_PAID


def payment_calculation(order: Mapping[str, Any]) -> dict[str, Any]:
    """One authoritative money calculation shared by the portal, admin views, and payment history.

    Only records in ``order["payments"]`` count: a pending submission, an OCR extraction, or an edited browser
    amount never changes these figures. The exact balance can be negative when the shop confirmed an excess;
    the displayed balance is clamped to zero and the excess is reported separately for admin review.
    """
    total = order.get("total_amount")
    total_value = round(float(total), 2) if total is not None else None
    paid = paid_amount(order)
    excess = pending_excess_amount(order)
    cod = is_cod(order)
    required = 0.0 if cod or total_value is None else required_down_payment(order)
    exact_balance = round(max(0.0, total_value - paid), 2) if total_value is not None else None
    down_shortfall = round(max(0.0, required - paid), 2) if total_value is not None else None
    return {
        "total_order_amount": total_value,
        "required_down_payment": round(required, 2),
        "confirmed_payments": paid,
        "amount_paid": paid,
        "pending_payments": pending_submission_amount(order),
        "remaining_balance": exact_balance,
        # The exact remainder is also exposed under the explicit name used by payment verification and reports.
        "remaining_balance_exact": exact_balance,
        "balance_due": exact_balance,
        "excess_amount": excess,
        "excess_pending_review": excess,
        "payment_percentage": round((min(paid, total_value) / total_value) * 100, 1) if total_value else 0.0,
        "additional_needed_for_down_payment": down_shortfall,
        "down_payment_shortfall": down_shortfall,
        "payment_status": payment_status_from_amounts(
            total_value, required, paid, cod=cod, excess=excess
        ),
        "down_payment_met": bool(total_value is not None and (cod or paid >= required - MONEY_TOLERANCE)),
    }


# An extraction failure is a review problem, never a money decision. These flags come from the OCR layer only;
# a receipt that says "Failed" is a different check and is handled by the verification comparison.
OCR_FAILURE_FLAGS = {"ocr_error", "engine_unavailable", "unreadable", "unsupported_type"}
OCR_FAILURE_MESSAGE = (
    "Unable to automatically read your receipt. Your uploaded receipt has been saved and will be reviewed by "
    "the shop. You may also enter your payment details manually."
)


def ocr_flags(ocr: Any) -> set[str]:
    if isinstance(ocr, Mapping):
        return {str(flag) for flag in (ocr.get("flags") or [])}
    return {str(flag) for flag in (getattr(ocr, "flags", None) or [])}


def ocr_amount_detected(ocr: Any) -> bool:
    fields = ocr.get("fields") if isinstance(ocr, Mapping) else getattr(ocr, "fields", None)
    return bool(str((fields or {}).get("amount") or "").strip())


def ocr_failure_detected(ocr: Any, verification: Any = None) -> bool:
    """True only when the shop could not read the receipt, not when the receipt says a payment failed."""
    flags = ocr_flags(ocr)
    if flags & OCR_FAILURE_FLAGS:
        return True
    if "failed_status" in flags or "conflicting_status" in flags:
        return False
    if isinstance(verification, Mapping) and not ocr_amount_detected(ocr):
        fields = ocr.get("fields") if isinstance(ocr, Mapping) else getattr(ocr, "fields", None)
        if str((fields or {}).get("status") or "").casefold() == "failed":
            return False
        return str(verification.get("status") or "").casefold() == verification_services.ADMIN_REVIEW
    return False


def customer_payment_status_label(order: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> str:
    """One of the customer-facing payment states, without changing the canonical stored status.

    ``item`` scopes the label to one history row; without it the helper describes the order as a whole.
    """
    if item is None:
        pending = pending_submissions(order)
        if pending:
            if any(ocr_failure_detected(entry.get("ocr"), entry.get("verification")) for entry in pending):
                return "OCR Failed – Manual Review Required"
            return "Payment Verification Pending"
        history = submissions(order)
        if history:
            latest = history[-1]
            if latest.get("status") == "rejected":
                return "Payment Rejected"
            if latest.get("status") == "receipt_requested" and ocr_failure_detected(latest.get("ocr"), latest.get("verification")):
                return "OCR Failed – Manual Review Required"
    status = payment_calculation(order)["payment_status"]
    if status == FULLY_PAID:
        return "Fully Paid"
    if status in {DOWN_PAYMENT_PAID, PARTIALLY_PAID, EXCESS_PAYMENT}:
        return "Down Payment Satisfied"
    return status


def work_approval(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("work_approval")
    return json_copy(value) if isinstance(value, dict) else {}


def work_approved(order: Mapping[str, Any]) -> bool:
    """Whether the customer accepted the finished work (or, for older orders, an approved draft).

    Once a finished-work preview has been sent (status pending or revision_requested), only an explicit
    approval of that preview unlocks the final payment. Orders from before this workflow keep their earlier
    draft/printing approval as the equivalent record.
    """
    record = work_approval(order)
    state = str(record.get("status") or "")
    if state == "approved" and record.get("approved_at"):
        return True
    if state in {"pending", "revision_requested"}:
        return False
    drafts = order.get("drafts") or []
    if isinstance(drafts, list) and any(isinstance(item, dict) and item.get("status") == "approved" for item in drafts):
        return True
    # Older orders were approved for printing when their draft was accepted; the audit history is their proof.
    return order["status"] in PRODUCTION_STATUSES | {
        "Approved for Printing", "Awaiting Full Payment", "Ready for Pickup", "Ready for Dispatch",
        "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed", "Completed",
    }


def work_status(order: Mapping[str, Any]) -> str:
    record = work_approval(order)
    state = str(record.get("status") or "")
    if state == "approved":
        return "Approved by Customer"
    if state == "revision_requested":
        return "Revision Requested"
    if state == "pending":
        return "Ready for Customer Review"
    if order["status"] in {"Ready for Customer Review", "Revision Requested", "Approved by Customer", "Awaiting Final Payment", "In Progress"}:
        return order["status"]
    return "In Progress" if down_payment_met(order) else "Awaiting Down Payment"


def final_payment_lock(order: Mapping[str, Any]) -> str:
    """Why the remaining-balance button is locked, or "" when the customer may pay it."""
    if order.get("total_amount") is None or is_cod(order) or not payment_option(order):
        return "The final amount is set after the shop confirms the order."
    if not down_payment_met(order):
        return "The required down payment must be verified first."
    # The work-approval gate is additive: only orders that actually use a finished-work preview are held here.
    # Older draft-first orders keep their existing payment flow (their approved draft is their approval record).
    if work_approval_lock(order):
        return "Approve the finished work before paying the remaining balance."
    return ""


def work_approval_lock(order: Mapping[str, Any]) -> bool:
    """Whether finished work is still waiting for the customer's decision.

    An explicit preview is the normal trigger. The new review statuses also keep the balance locked so a
    manual status change cannot unlock payment without approval. Older draft-first statuses never enter
    this group, so they keep their existing payment flow.
    """
    if work_approved(order):
        return False
    return bool(work_approval(order).get("sent_at")) or order["status"] in WORK_REVIEW_STATUSES


def ensure_final_payment_allowed(order: Mapping[str, Any]) -> None:
    """Server-side gate: no remaining-balance money may move before the customer approves the finished work."""
    if final_payment_lock(order):
        raise HTTPException(status_code=409, detail=final_payment_lock(order))


def payment_status(order: Mapping[str, Any]) -> str:
    """Payment-only status: derived from confirmed money, never from production progress or OCR.

    A receipt waiting for verification is shown separately as ``pending_verification``; it does not turn a
    No Payment order into a paid one. Work readiness belongs in ``work_status`` and ``final_payment_lock``.
    """
    total = order.get("total_amount")
    if total is None:
        return NO_PAYMENT
    cod = is_cod(order)
    return payment_status_from_amounts(
        total, 0.0 if cod else down_payment_due(order), paid_amount(order),
        cod=cod, excess=pending_excess_amount(order),
    )


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
    # its quotation exists, and an estimate is never turned into an amount the customer must pay. An
    # administrator-set amount overrides the original 50% calculation for this order.
    required = 0.0 if cod else (round(required_down_payment(order), 2) if total is not None else None)
    if cod:
        status = "Not required (COD)"
    elif total is None:
        status = "Due after quotation"
    elif not price_agreed(order):
        status = "After quotation acceptance" if not is_fixed_price(order) else "After order approval"
    elif paid >= required - 0.005:
        status = "Paid"
    elif pending > 0:
        status = "Pending Verification"
    elif paid > 0:
        status = "Partially paid"
    else:
        status = "Pending"
    balance = round(max(0.0, float(total) - paid), 2) if total is not None else None
    calculation = payment_calculation(order)
    shipping = shipping_view(order)
    fee = shipping["shipping_fee"]
    return {
        "delivery_fee": fee,
        "shipping_fee": fee,
        "delivery_province": shipping["delivery_province"],
        "destination_region": shipping["destination_region"],
        "estimated_delivery": shipping["estimated_delivery"],
        # Product, package, and customization charges (the agreed total without the delivery fee).
        "subtotal": round(float(total) - fee, 2) if total is not None else None,
        "order_subtotal": round(float(total) - fee, 2) if total is not None else None,
        "total_amount": round(float(total), 2) if total is not None else None,
        "refunded": refunded_amount(order),
        "payment_option": option,
        "payment_option_label": payment_option_label(order),
        "payment_option_chosen": bool(option),
        "cod_allowed": cod_allowed(order),
        "down_payment_rate": 0.0 if cod else (round(required / float(total), 4) if total else DOWN_PAYMENT_RATE),
        "admin_set_down_payment": order.get("required_down_payment") is not None,
        "confirmed_total": round(float(total), 2) if total is not None else None,
        "estimated_total": estimate,
        "based_on_estimate": total is None and estimate is not None,
        "down_payment_required": required,
        "down_payment_remaining": round(max(0.0, required - paid), 2) if required is not None else None,
        "down_payment_status": status,
        "amount_paid": paid,
        "pending_verification": pending,
        "balance": balance,
        # The complete, shared calculation: total − confirmed payments, the exact (possibly negative)
        # balance, the excess requiring review, and how much more is needed to reach the down payment.
        "payment_calculation": calculation,
        "remaining_balance_exact": calculation["remaining_balance"],
        "excess_amount": calculation["excess_amount"],
        "additional_needed_for_down_payment": calculation["additional_needed_for_down_payment"],
        # The other 50%: what is still to be paid after the down payment (everything, for older COD orders).
        "remaining_after_down_payment": round(float(total) - (required or 0.0), 2) if total is not None else None,
        # What the customer pays next and why; set by the server so the amount cannot be changed by the customer.
        "next_payment": next_customer_payment(order),
        # New workflow fields: payment progress is independent of work progress.
        "payment_status": payment_status(order),
        # The short, customer-facing wording used by the portal. Canonical statuses remain unchanged so older
        # reports and filters keep working.
        "customer_status_label": customer_payment_status_label(order),
        "work_status": work_status(order),
        "down_payment_verified": down_payment_met(order),
        "work_approved": work_approved(order),
        "final_payment_unlocked": not bool(final_payment_lock(order)) and balance > 0.005 if balance is not None else False,
        "final_payment_locked": bool(final_payment_lock(order)),
        "final_payment_lock": final_payment_lock(order),
        "remaining_balance": balance,
    }


def work_approval_view(order: Mapping[str, Any], admin: bool = False) -> dict[str, Any]:
    """The finished-work review record, including the preview files the customer is approving."""
    record = work_approval(order)
    prefix = f"/api/admin/orders/{order['reference']}/files" if admin else f"/api/orders/{order['reference']}/files"
    files = []
    for item in record.get("preview_files") or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        files.append({
            "id": item["id"],
            "name": item.get("name", ""),
            "mime_type": item.get("mime_type", ""),
            "size": item.get("size", 0),
            "url": f"{prefix}/{item['id']}",
        })
    return {
        "status": record.get("status", ""),
        "status_label": WORK_APPROVAL_LABELS.get(record.get("status", ""), record.get("status", "")),
        "preview_files": files,
        "remarks": record.get("remarks", ""),
        "sent_at": record.get("sent_at"),
        "responded_at": record.get("responded_at"),
        "approval_note": record.get("approval_note", ""),
        "revision_note": record.get("revision_note", ""),
        "revision_count": int(record.get("revision_count") or 0),
        "approved": work_approved(order),
        "work_status": work_status(order),
    }


WORK_APPROVAL_LABELS = {
    "pending": "Waiting for customer review",
    "approved": "Approved by customer",
    "revision_requested": "Revision requested",
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
        # Current progress is a separate dimension from the forward-looking release destination.
        "fulfillment_status": fulfillment_status(order),
        "release_ready": release_ready(order),
        # Kept for compatibility; ``next_release_status`` is the explicit name for the same value.
        "release_status": release_status(order),
        "next_release_status": next_release_status(order),
        **fulfillment_services.fulfillment_details(order, admin),
        # The shipping fields are a snapshot: later fee changes never rewrite an existing order.
        **shipping_view(order),
    }


def _submission_confirmation_view(
    order: Mapping[str, Any],
    item: Mapping[str, Any],
    verification: Mapping[str, Any],
    breakdown: Mapping[str, Any],
) -> dict[str, Any]:
    """Everything the Confirm Received Payment modal needs to compare a receipt with the order.

    This is advisory. Every status here describes what OCR *read* and how it compares with the shop's
    records; none of it means money was received. Only the administrator's explicit confirmation can do that.
    """
    ocr = item.get("ocr") if isinstance(item.get("ocr"), Mapping) else {}
    fields = ocr.get("fields") if isinstance(ocr, Mapping) and isinstance(ocr.get("fields"), Mapping) else {}
    flags = set(verification.get("flags") or [])
    checks = {
        str(check.get("key")): check
        for check in (verification.get("checks") or []) if isinstance(check, Mapping)
    }

    def amount(value: Any) -> float | None:
        try:
            return round(float(str(value).replace(",", "")), 2)
        except (TypeError, ValueError):
            return None

    expected = amount(item.get("amount")) or 0.0
    extracted = amount(fields.get("amount"))
    difference = round(extracted - expected, 2) if extracted is not None else None
    shortfall = round(expected - extracted, 2) if extracted is not None and extracted < expected - 0.005 else 0.0
    amount_state = str(checks.get("amount", {}).get("state") or "missing")
    reference_state = str(checks.get("reference", {}).get("state") or "missing")
    duplicate_state = str(checks.get("duplicate", {}).get("state") or "match")
    unreadable = bool(flags & {"engine_unavailable", "ocr_error", "unreadable", "unsupported_type", "low_confidence"})
    needs_review = unreadable or verification.get("status") in {
        verification_services.ADMIN_REVIEW, verification_services.DUPLICATE, verification_services.REJECTED,
    }
    reference_text = str(fields.get("reference") or "").strip()
    key_fields_read = bool(str(fields.get("amount") or "").strip() or reference_text)
    has_fields = any(str(value or "").strip() for value in fields.values())
    statuses: list[dict[str, str]] = []
    result = str(verification.get("result") or "")
    result_label = str(verification.get("result_label") or "")
    if not result:
        # Older submissions predate the result codes: derive the same customer-facing outcome.
        if duplicate_state == "inconsistent":
            result = verification_services.RESULT_DUPLICATE
        elif reference_state == "inconsistent":
            result = verification_services.RESULT_REFERENCE_MISMATCH
        elif shortfall > 0:
            result = verification_services.RESULT_INSUFFICIENT
        elif needs_review or not has_fields:
            result = verification_services.RESULT_MANUAL_REVIEW
        else:
            result = verification_services.RESULT_OCR_PENDING
        result_label = verification_services.RESULT_LABELS[result]

    calculation = verification.get("down_payment") if isinstance(verification.get("down_payment"), Mapping) else {}
    if not calculation:
        calculation = verification_services.down_payment_calculation(
            order, expected, extracted, float(breakdown.get("amount_paid") or 0) if str(item.get("status") or "") != "verified" else 0.0,
        )

    # One clear primary outcome first, then the supporting check chips.
    primary_tone = {
        verification_services.RESULT_VERIFIED: "success",
        verification_services.RESULT_OCR_PENDING: "info",
        verification_services.RESULT_INSUFFICIENT: "danger",
        verification_services.RESULT_REFERENCE_MISMATCH: "danger",
        verification_services.RESULT_DUPLICATE: "danger",
        verification_services.RESULT_MANUAL_REVIEW: "warning",
        verification_services.RESULT_REJECTED: "danger",
    }.get(result, "info")
    statuses.append({"key": "verification_result", "label": result_label, "tone": primary_tone})
    if not has_fields:
        statuses.append({"key": "ocr_processing", "label": "OCR Processing", "tone": "warning"})
    elif key_fields_read and not unreadable:
        statuses.append({"key": "ocr_extracted", "label": "OCR Successfully Extracted", "tone": "success"})
    else:
        statuses.append({"key": "manual_review", "label": "Needs Manual Review", "tone": "warning"})

    if amount_state == "match":
        statuses.append({"key": "amount_matched", "label": "Amount Matched", "tone": "success"})
    elif extracted is None:
        statuses.append({"key": "amount_mismatch", "label": "Amount Mismatch · Not read", "tone": "warning"})
    else:
        suffix = f" · Short by PHP {shortfall:,.2f}" if shortfall else ""
        statuses.append({"key": "amount_mismatch", "label": f"Amount Mismatch{suffix}", "tone": "danger"})

    if duplicate_state == "inconsistent":
        statuses.append({"key": "duplicate_reference", "label": "Duplicate Reference Number", "tone": "danger"})
    elif reference_state == "match" and len(_payment_reference_key(reference_text)) >= 4:
        statuses.append({"key": "reference_matched", "label": "Reference Matched", "tone": "success"})
    elif reference_state == "missing" or not reference_text:
        statuses.append({"key": "reference_missing", "label": "Reference Missing or Unreadable", "tone": "warning"})
    else:
        statuses.append({"key": "reference_mismatch", "label": "Reference Number Mismatch", "tone": "danger"})

    confirmed = str(item.get("status") or "") == "verified"
    status_after = str(calculation.get("payment_status") or "")
    if status_after:
        statuses.append({
            "key": "payment_status",
            "label": f"Payment status if confirmed: {status_after}",
            "tone": "danger" if status_after == EXCESS_PAYMENT else "success" if status_after in {FULLY_PAID, DOWN_PAYMENT_PAID} else "info",
        })
    statuses.append({
        "key": "confirmed_received" if confirmed else "awaiting_confirmation",
        "label": "Confirmed Received" if confirmed else "Awaiting Admin Confirmation",
        "tone": "success" if confirmed else "info",
    })

    return {
        "verification_status": str(verification.get("status") or ""),
        "verification_status_label": str(verification.get("status_label") or ""),
        "verification_result": result,
        "verification_result_label": result_label,
        "verification_date": verification.get("reviewed_at") or item.get("reviewed_at"),
        "verification_basis_label": str(verification.get("verification_basis_label") or ""),
        "total_order_amount": calculation.get("total_order_amount"),
        "required_down_payment": calculation.get("required_down_payment"),
        "stage_amount_due": calculation.get("stage_amount_due", expected),
        "actual_amount_paid": extracted,
        "previously_paid": calculation.get("previously_paid"),
        "cumulative_paid": calculation.get("cumulative_paid"),
        "remaining_down_payment": calculation.get("remaining_down_payment"),
        "remaining_balance_after": calculation.get("remaining_balance"),
        "payment_percentage": calculation.get("payment_percentage"),
        "down_payment_percentage": calculation.get("down_payment_percentage"),
        "insufficient_by": calculation.get("insufficient_by"),
        "excess_amount": calculation.get("excess_amount"),
        "payment_status": status_after,
        "additional_needed_for_down_payment": calculation.get("down_payment_shortfall"),
        "order_total": breakdown.get("total"),
        "down_payment_required": breakdown.get("down_payment_required"),
        "previously_received": breakdown.get("amount_paid"),
        "remaining_balance": breakdown.get("balance"),
        "expected_amount": expected,
        "extracted_amount": extracted,
        "amount_difference": difference,
        "amount_shortfall": shortfall,
        "amount_match": amount_state == "match",
        "amount_state": amount_state,
        "reference_state": reference_state,
        "reference_matched": reference_state == "match" and duplicate_state != "inconsistent" and len(_payment_reference_key(reference_text)) >= 4,
        "reference_duplicate": duplicate_state == "inconsistent",
        "reference_missing": reference_state == "missing" or not reference_text,
        "duplicate_state": duplicate_state,
        "extracted_reference": reference_text,
        "extracted_method": str(fields.get("method") or ""),
        "extracted_date_time": str(fields.get("date_time") or ""),
        "extracted_sender": str(fields.get("sender") or ""),
        "extracted_recipient": str(fields.get("recipient") or ""),
        "ocr_status": "processing" if not has_fields else ("extracted" if key_fields_read and not unreadable else "manual_review"),
        "payment_status_label": "Confirmed Received" if confirmed else "Awaiting Admin Confirmation",
        "statuses": statuses,
        "notice": "OCR reads text only. It does not prove that money was received; confirm the shop's account before recording this payment.",
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
    # Explicit order-level progress fields. They are deliberately separate from the production status.
    result["payment_status"] = payment_status(order)
    result["work_status"] = work_status(order)
    result["work_approval"] = work_approval_view(order, admin)
    result["required_down_payment"] = required_down_payment(order) if order.get("total_amount") is not None else None
    result["fixed_price"] = is_fixed_price(order)
    result["price_basis"] = price_basis(order)
    result["fixed_price_label"] = fixed_price_label(order)
    result["price_list"] = customization.get("price_list")
    result["shipping"] = shipping_view(order)
    result["fulfillment"] = fulfillment_view(order, admin)
    stored_quote = quotation(order)
    result["quotation"] = {
        **shipping_view(order),
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
        "down_payment_amount": stored_quote.get("down_payment_amount"),
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
    # Who recorded or verified a payment is for the shop's records only. A payment that came from a customer
    # submission keeps its original receipt and OCR record linked, so the admin modal can show both side by side
    # without creating a second payment system or duplicating stored files.
    linked_submissions = {str(item.get("id")): item for item in submissions(order)}
    result["payments"] = []
    for index, payment in enumerate(order["payments"], start=1):
        view_payment = {
            **{key: value for key, value in payment.items() if admin or key not in {"recorded_by", "verification"}},
            "number": index,
        }
        if admin:
            verification_view = payment_verification(payment)
            view_payment["verification_view"] = verification_view
            linked = linked_submissions.get(str(payment.get("submission_id") or ""), {})
            proof = linked.get("proof") if isinstance(linked.get("proof"), Mapping) else {}
            stored_verification = linked.get("verification") if isinstance(linked.get("verification"), Mapping) else {}
            ocr = linked.get("ocr") if isinstance(linked.get("ocr"), Mapping) else {}
            view_payment["receipt"] = {
                "name": str(proof.get("name") or ""),
                "mime_type": str(proof.get("mime_type") or ""),
                "url": url(str(proof["id"])) if proof.get("id") else "",
            } if proof else None
            view_payment["ocr"] = ocr or (payment.get("ocr") if isinstance(payment.get("ocr"), Mapping) else {})
            view_payment["extraction_verification"] = stored_verification
            view_payment["verification_date"] = verification_view.get("verified_at")
            view_payment["extraction_comparison"] = verification_services.comparison_view(
                order, {"ocr": ocr, "proof": proof, "reference": payment.get("reference"),
                        "amount": payment.get("amount"), "method": payment.get("method")},
                stored_verification or {"status": verification_view.get("status", "")},
            ) if ocr else None
        result["payments"].append(view_payment)
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
        item_ocr = item.get("ocr") or {}
        item_ocr_failed = ocr_failure_detected(item_ocr, stored_verification)
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
            "excess_amount": item.get("excess_amount", 0.0),
            "requires_admin_review": bool(item.get("requires_admin_review", False)),
            "ocr": item_ocr,
            "ocr_failed": item_ocr_failed,
            "amount_detected": ocr_amount_detected(item_ocr),
            "manual_review_required": item_ocr_failed or str(stored_verification.get("status") or "") in {
                verification_services.ADMIN_REVIEW,
                verification_services.DUPLICATE,
                verification_services.REJECTED,
            },
            "ocr_failure_message": OCR_FAILURE_MESSAGE if item_ocr_failed else "",
            "customer_status_label": (
                "Payment Rejected" if item.get("status") == "rejected"
                else "OCR Failed – Manual Review Required" if item_ocr_failed and item.get("status") != "verified"
                else "Payment Verification Pending" if item.get("status") in {"pending", "receipt_requested"}
                else customer_payment_status_label(order, item)
            ),
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
            view["confirmation"] = _submission_confirmation_view(
                order, view, stored_verification or {"status": view["verification_status"]}, result["payment"],
            )
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
        label = "Required down payment" if order.get("required_down_payment") is not None else "50% down payment"
        # Keep the original shape for down payments: only the remaining balance can be locked by work review.
        return {"stage": "down_payment", "label": label, "amount": remaining_down}
    lock = final_payment_lock(order)
    return {
        "stage": "balance",
        "label": "Remaining balance",
        "amount": balance,
        "locked": bool(lock),
        "reason": lock,
    }


def order_after_update(order: OrderRecord | None, admin: bool = False) -> dict[str, Any]:
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order_view(order, admin)


def order_files(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every file stored with an order: uploads, drafts, payment proofs, production and delivery photos, refund proofs."""
    from backend.app.services import fulfillment_services, refund_services

    attachments = list(order["attachments"]) + ([order["draft_attachment"]] if order["draft_attachment"] else [])
    attachments += [item["proof"] for item in submissions(order) if item.get("proof")]
    attachments += [item for item in (work_approval(order).get("preview_files") or []) if isinstance(item, dict)]
    attachments += fulfillment_services.files(order)
    attachments += refund_services.files(order)
    return [item for item in attachments if isinstance(item, dict) and item.get("id")]


def attachment_response(order: Mapping[str, Any], file_id: str, inline: bool = False) -> Response:
    attachment = next((item for item in order_files(order) if item["id"] == file_id), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="File not found.")
    return file_services.response(attachment["filename"], attachment["mime_type"], attachment["name"], inline=inline)


def clean_fulfillment(value: Any) -> dict[str, Any]:
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
    quote = shop_settings_services.delivery_quote(cleaned["province"], value.get("area_id"))
    cleaned.update(quote)
    # Immutable per-order shipping snapshot: province, island group, rate, and estimate are frozen at
    # checkout. Later admin rate changes only affect orders placed after the change.
    cleaned["shipping"] = {
        "province": address["province"],
        "city": address["city"],
        "region": quote["region"],
        "region_label": quote["region_label"],
        "destination_region": quote["region_label"],
        "delivery_fee": quote["delivery_fee"],
        "estimate_min_days": quote["estimate_min_days"],
        "estimate_max_days": quote["estimate_max_days"],
        "estimate_text": quote["estimate_text"],
        "area_id": quote["area_id"],
        "area_label": quote["area_label"],
        "calculated_at": now_iso(),
        "snapshot_recorded": True,
    }
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
            "shipping": fulfillment_choice.get("shipping", {}),
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


def workflow_state(order: Mapping[str, Any]) -> dict[str, Any]:
    """A small read-only snapshot so open pages can detect changes without reloading full orders.

    ``version`` is the same counter returned with every order view and sent with admin actions. Every
    field here comes from the stored order: a polling response never decides what the next workflow step
    is, it only tells the page whether it should fetch the authoritative order again.
    """
    return {
        "reference": order["reference"],
        "version": order_repository.order_version(order),
        "status": order["status"],
        "payment_status": payment_status(order),
        "work_status": work_status(order),
        "fulfillment_status": fulfillment_status(order),
        "work_approval_status": work_approval_view(order).get("status", ""),
        "pending_submissions": len(pending_submissions(order)),
        "confirmed_payments": round(payments_total(order["payments"]), 2),
        "updated_at": order["updated_at"],
    }


def account_order_state(customer_id: str, reference: str) -> dict[str, Any]:
    """Ownership-checked snapshot for the customer's open order."""
    return workflow_state(customer_owned_order(reference, None, customer_id))


def admin_order_state(reference: str) -> dict[str, Any]:
    """Admin-only snapshot for the order currently open in a modal or verification view."""
    order = order_repository.get_order(reference)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return workflow_state(order)


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
    audit: dict[str, Any] | list[dict[str, Any]] | None = None,
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
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise HTTPException(status_code=422, detail="Order version must be a positive whole number.")
        version = expected_version
    notifications = notifications or []
    # Payment progress is stored next to the order, so the admin list can filter it without rebuilding every
    # order. Deriving it here keeps it current after every action that can change money or work approval.
    if "payment_status" not in updates and order.get("total_amount") is not None:
        projected = dict(order)
        projected.update(updates)
        updates["payment_status"] = payment_status(projected)
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


def _receipt_file_key(value: Any) -> str:
    """Normalized fingerprint of one uploaded receipt file."""
    return str(value or "").strip().casefold()


def _ensure_receipt_unused(receipt_hash: Any, ignore_payment_id: str | None = None) -> None:
    """A verified payment cannot be backed by a receipt file that already credited another payment.

    Duplicate files may be *submitted* for review (the admin table flags them), but a second verification
    from the same file would credit the same proof of payment twice. Pending and rejected submissions are
    not money, so they do not block verification here; the database claim taken on verification is the
    atomic guard against two concurrent verifications of the same file.
    """
    key = _receipt_file_key(receipt_hash)
    if not key:
        return
    for other in order_repository.list_orders():
        for payment in other["payments"]:
            if str(payment.get("id") or "") == ignore_payment_id:
                continue
            if _receipt_file_key(payment.get("receipt_hash")) == key:
                raise HTTPException(
                    status_code=409,
                    detail="This receipt file was already used for a verified payment on another order. One receipt cannot pay twice.",
                )


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
    ocr = ocr_services.extract_receipt(
        contents, str(metadata.get("mime_type") or ""), name=str(metadata.get("name") or ""), expected_method=method,
    )
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


def minimum_customer_payment(due: Mapping[str, Any]) -> float:
    """The smallest amount the customer form accepts for the current stage.

    While the down payment is outstanding the customer must pay at least the remaining down payment the
    server calculated; a larger amount is always accepted and credited in full. Once the down payment is
    covered, any positive amount toward the balance may still be submitted.
    """
    if due.get("stage") != "down_payment":
        return 0.01
    return round(max(0.0, float(due.get("amount") or 0.0)), 2)


def ensure_customer_payment_minimum(amount: float, due: Mapping[str, Any]) -> None:
    minimum = minimum_customer_payment(due)
    if amount < minimum - MONEY_TOLERANCE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The payment cannot be below the remaining down payment of PHP {minimum:,.2f}. "
                "Pay that amount or more; amounts above it are accepted."
            ),
        )


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
    if due.get("locked"):
        raise HTTPException(status_code=409, detail=due["reason"])
    method = shop_settings_services.default_provider() if payload.get("method") in (None, "") else shop_settings_services.active_provider(payload.get("method"))
    if payload.get("proof") in (None, ""):
        raise HTTPException(status_code=422, detail=f"Upload the {method} payment screenshot or receipt.")
    metadata, contents = _decode_receipt(payload.get("proof"))
    reference_text = _text(payload.get("reference", "") or "", "Payment reference", 120)
    declared = payload.get("amount")
    if not _valid_amount(declared) or float(declared) <= 0:
        declared = due["amount"]
    declared = round(float(declared), 2)
    ensure_customer_payment_minimum(declared, due)
    submission = {"id": "preview", "reference": reference_text, "amount": declared, "receipt_hash": ocr_services.receipt_hash(contents)}
    ocr_view, verification = _scan_receipt(order, submission, metadata, contents, method, due)
    checks = {item.get("key"): item for item in (verification.get("checks") or []) if isinstance(item, dict)}
    duplicate_check = checks.get("duplicate", {})
    amount_check = checks.get("amount", {})
    reference_field = str((ocr_view.get("fields") or {}).get("reference") or "").strip()
    ocr_failed = ocr_failure_detected(ocr_view, verification)
    amount_detected = ocr_amount_detected(ocr_view)
    ocr_status = "Failed" if ocr_failed else "Detected"
    duplicate_reference = duplicate_check.get("state") == "inconsistent"
    reference_message = ""
    if reference_field:
        reference_message = (
            "This reference number has already been submitted. Please check your payment details or upload a different transaction receipt."
            if duplicate_reference
            else "Reference number detected. Please review your payment details before submitting."
        )
    return {
        "reference": order["reference"],
        "method": method,
        "required_amount": due["amount"],
        "stage": due["stage"],
        "label": due["label"],
        "ocr_status": ocr_status,
        "ocr_failed": ocr_failed,
        "amount_detected": amount_detected,
        "manual_review_required": ocr_failed or str(verification.get("status") or "") in {
            verification_services.ADMIN_REVIEW,
            verification_services.DUPLICATE,
            verification_services.REJECTED,
        },
        "ocr_failure_message": OCR_FAILURE_MESSAGE if ocr_failed else "",
        "customer_status_label": "OCR Failed – Manual Review Required" if ocr_failed else "Payment Verification Pending",
        "ocr": ocr_view,
        "verification": verification,
        "duplicate_reference": duplicate_reference,
        "duplicate_message": (
            "This reference number has already been submitted. Please check your payment details or upload a different transaction receipt."
            if duplicate_reference else ""
        ),
        "reference_message": reference_message,
        "amount_match": amount_check.get("state") == "match",
        "amount_message": amount_check.get("detail", ""),
        "down_payment": verification.get("down_payment") or payment_calculation(order),
        "payment_status_label": payment_status(order),
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
    if due.get("locked"):
        raise HTTPException(status_code=409, detail=due["reason"])
    declared = payload.get("amount")
    if declared not in (None, ""):
        # The minimum is the remaining down payment calculated by the server; anything above it is accepted
        # and credited in full after an administrator confirms the money actually arrived.
        if not _valid_amount(declared) or float(declared) <= 0:
            raise HTTPException(status_code=422, detail="Enter the amount actually paid.")
        declared = round(float(declared), 2)
        ensure_customer_payment_minimum(declared, due)
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
        "amount": declared if declared is not None else due["amount"],
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
    # An explicit amount wins; otherwise use what OCR actually read from the receipt; the shop's suggested
    # amount is only the fallback. None of these values credits money before the admin confirms it.
    if declared is None:
        try:
            extracted = round(float(str((ocr_view.get("fields") or {}).get("amount") or "").replace(",", "")), 2)
        except (TypeError, ValueError):
            extracted = None
        if extracted is not None and extracted > 0:
            submission["amount"] = extracted
    # A receipt that reads below the down payment cannot be submitted as a down payment. The customer must
    # correct the amount; an unreadable receipt falls back to the server-calculated amount, which meets it.
    ensure_customer_payment_minimum(float(submission["amount"]), due)
    review = _excess_review(order, float(submission["amount"]))
    submission.update(review)
    if review["requires_admin_review"]:
        verification["status"] = verification_services.ADMIN_REVIEW
        verification["status_label"] = verification_services.STATUS_LABELS[verification_services.ADMIN_REVIEW]
        verification["auto_confirm"] = False
        verification["flags"] = list(verification.get("flags") or []) + ["overpayment"]
    submission["verification"] = verification
    submission["verification_status"] = verification["status"]
    submission["verification_method"] = verification["verification_method"]
    try:
        file_services.save(proof, contents)  # type: ignore[arg-type]
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(status_code=500, detail="The proof of payment could not be saved.") from error
    reported = float(submission["amount"])
    notifications = [notification_services.build(
        order, "payment_receipt_uploaded", f"payment-uploaded:{submission['id']}", "Payment receipt received",
        f"We received your {method} receipt of PHP {reported:,.2f} for {due['label'].lower()} "
        f"(reference {reference_text}). It is being processed; no payment is counted until it is verified.",
    )]
    if review["requires_admin_review"]:
        notifications.append(notification_services.build(
            order, "payment_additional_verification", f"payment-review:{submission['id']}", "Payment needs additional verification",
            f"The reported amount of PHP {reported:,.2f} exceeds the order's remaining balance by "
            f"PHP {review['excess_amount']:,.2f}. The shop will review the excess before confirming anything.",
        ))
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
            f"Customer submitted a payment of PHP {reported:,.2f} ({due['label'].lower()}) via {method} "
            f"(reference {reference_text}) with a screenshot/receipt. OCR classification: {verification['status_label']} "
            f"(confidence {verification['confidence']:.0%}); this is not proof that funds were received."
            + _excess_suffix(review),
            audit=_audit(
                "payment.submitted", CUSTOMER, customer_id,
                submission_id=submission["id"], amount=reported, stage=due["stage"], method=method, reference=reference_text,
                ocr_status=status, ocr_confidence=verification["confidence"], verification_method=verification["verification_method"],
                excess_amount=review["excess_amount"], requires_admin_review=review["requires_admin_review"],
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
        confirm_payload = {"received": True, "amount": reported}
        actor = verification["provider"].get("provider") or "payment provider"

        def confirm(expected: int | None = None) -> None:
            body = dict(confirm_payload)
            if expected is not None:
                body["expected_version"] = expected
            verify_payment_submission(reference, submission["id"], body, actor=actor)

        try:
            confirm()
        except HTTPException as error:
            if error.status_code != 409 or "changed by another action" not in str(error.detail):
                logger.warning("Provider-confirmed payment could not be recorded for %s: %s", reference, error.detail)
            else:
                # A concurrent admin action landed between submission and verification. Retry once against
                # the freshly read version instead of silently leaving provider-confirmed money pending.
                try:
                    fresh = order_repository.get_order(reference)
                    confirm(order_repository.order_version(fresh) if fresh is not None else None)
                except HTTPException as retry_error:
                    logger.warning(
                        "Provider-confirmed payment still could not be recorded for %s: %s",
                        reference, retry_error.detail,
                    )
                else:
                    return order_after_update(order_repository.get_order(reference), admin=False)
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


def payment_applied_amount(payment: Mapping[str, Any]) -> float:
    """The part of one received payment that is actually credited to the order total.

    ``amount`` stays the full money received for the audit trail. New records split it into
    ``applied_amount`` (credited) and ``excess_amount`` (held for review). Older records without
    ``applied_amount`` are interpreted from their stored excess so historical overpayments are not
    silently credited either.
    """
    raw = float(payment.get("amount") or 0)
    if "applied_amount" in payment and payment.get("applied_amount") is not None:
        return round(max(0.0, float(payment.get("applied_amount") or 0)), 2)
    excess = float(payment.get("excess_amount") or 0) if payment.get("requires_admin_review") else 0.0
    return round(max(0.0, raw - excess), 2)


def payments_total(payments: Iterable[Mapping[str, Any]]) -> float:
    return round(sum(payment_applied_amount(payment) for payment in payments), 2)


def pending_excess_amount(order: Mapping[str, Any]) -> float:
    """Money received above the order total that is still waiting for an explicit admin decision."""
    total = 0.0
    for payment in order.get("payments") or []:
        excess = float(payment.get("excess_amount") or 0) if isinstance(payment, Mapping) else 0.0
        resolved = str(payment.get("excess_review_status") or "") == "resolved" or bool(payment.get("excess_resolved_at"))
        if excess > MONEY_TOLERANCE and payment.get("requires_admin_review") and not resolved:
            total += excess
    return round(total, 2)


def paid_amount(order: Mapping[str, Any]) -> float:
    return payments_total(order.get("payments") or [])


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
    shipping = shipping_view(order)
    fee = shipping["shipping_fee"]
    total = round(subtotal + fee, 2)
    stored_down = validate_down_payment(payload.get("down_payment_amount"), total)
    down = stored_down if stored_down is not None else round(total * DOWN_PAYMENT_RATE, 2)
    down_text = f"Down payment PHP {down:,.2f}" if stored_down is not None else f"50% down payment PHP {down:,.2f}"
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
        "delivery_province": shipping["delivery_province"],
        "destination_region": shipping["destination_region"],
        "estimated_delivery": shipping["estimated_delivery"],
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
        "delivery_province": shipping["delivery_province"],
        "destination_region": shipping["destination_region"],
        "estimated_delivery": shipping["estimated_delivery"],
        "down_payment_amount": stored_down,
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
        f" {down_text}; remaining balance PHP {total - down:,.2f}. "
        f"Payment instructions were {'provided' if instructions else 'not included'}. Awaiting customer acceptance."
    )
    product = (order.get("product") or {}).get("name", "your order")
    if revised:
        title = "Revised quotation"
        message = (
            f"The shop revised the quotation for {product} from PHP {float(previous_amount or 0):,.2f} to PHP {total:,.2f}. "
            f"{down_text}. Review the latest quotation and accept or decline it in your customer portal."
        )
    else:
        title = "Quotation ready"
        message = (
            f"The shop sent a quotation of PHP {total:,.2f} for {product}. {down_text}. "
            "Review it and accept or decline it in your customer portal."
        )
    if note:
        message += f" Message from the shop: {note}"
    return _save(
        order,
        {
            "total_amount": total,
            "required_down_payment": stored_down,
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
    override = payload.get("total_amount")
    if override not in (None, ""):
        if isinstance(override, bool) or not isinstance(override, (int, float)) or not math.isfinite(override) or not 0 < float(override) <= 100000000:
            raise HTTPException(status_code=422, detail="Enter a valid total order amount.")
        total = round(float(override), 2)
    stored_down = validate_down_payment(payload.get("down_payment_amount"), total)
    down = stored_down if stored_down is not None else round(total * DOWN_PAYMENT_RATE, 2)
    down_text = f"Down payment PHP {down:,.2f}" if stored_down is not None else f"50% down payment PHP {down:,.2f}"
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
        {
            "status": "Awaiting Down Payment",
            "total_amount": total,
            "required_down_payment": stored_down,
            "payment_instructions": instructions,
            "payment_option": NEW_ORDER_PAYMENT_OPTION,
        },
        f"Administrator approved the {kind_text} at PHP {total:,.2f}. {down_text} is required "
        f"to confirm the order.{switched} Payment instructions were {shared}.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "order.approved", ADMIN, actor, total=total, down_payment=down,
            package=package.get("name", ""), standard_size=price_list.get("size", ""),
        ),
        notifications=[notification_services.build(
            order, "down_payment_requested", "down-payment-requested", title,
            f"The shop approved your {what} order (PHP {total:,.2f}). Pay the "
            f"{'50% down payment of ' if stored_down is None else 'down payment of '}PHP {down:,.2f} "
            "through GCash and upload the screenshot or receipt in your customer portal to confirm your order.",
        )],
    )


def set_payment_terms(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Set or revise the agreed total and required down payment before any money is verified.

    The quotation and package-approval actions already accept these values. This endpoint lets an
    administrator correct the terms while the order is still waiting for its down payment, without having to
    resend a quotation. Once money has been submitted or recorded the terms are locked.
    """
    order = _admin_order(reference)
    if order["payments"] or pending_submissions(order):
        raise HTTPException(status_code=409, detail="Payment terms cannot change after a payment was submitted or recorded.")
    if order["status"] not in {"Pending Confirmation", "Under Review", "Awaiting Down Payment"}:
        raise HTTPException(status_code=409, detail="Payment terms can only be changed before the down payment is verified.")
    total_value = payload.get("total_amount", order.get("total_amount"))
    if total_value in (None, ""):
        raise HTTPException(status_code=422, detail="Enter the total order amount.")
    if isinstance(total_value, bool) or not isinstance(total_value, (int, float)) or not math.isfinite(total_value) or not 0 < float(total_value) <= 100000000:
        raise HTTPException(status_code=422, detail="Enter a valid total order amount.")
    total = round(float(total_value), 2)
    down = validate_down_payment(payload.get("down_payment_amount"), total)
    instructions = _text(payload.get("payment_instructions", "") or "", "Payment instructions", 1000)
    stored_total = order.get("total_amount")
    stored_down = order.get("required_down_payment")
    stored_instructions = str(order.get("payment_instructions") or "")
    stored_total_value = round(float(stored_total), 2) if _valid_amount(stored_total) else None
    stored_down_value = round(float(stored_down), 2) if _valid_amount(stored_down) else stored_down
    changed = (
        stored_total_value != total
        or stored_down_value != down
        or stored_instructions != instructions
    )
    # A revision needs an explanation in the order history and the append-only audit log.
    note = _text(payload.get("note", "") or "", "Reason", 500, required=changed)

    # Project the values being saved through the authoritative breakdown so the saved event, audit entry,
    # and customer notification all describe the same figures the portal will show.
    projected = dict(order)
    projected.update({"total_amount": total, "required_down_payment": down})
    breakdown = payment_breakdown(projected)
    effective_down = round(float(breakdown["down_payment_required"]), 2)
    remaining_after = round(float(breakdown["remaining_after_down_payment"]), 2)
    subtotal = round(float(breakdown["subtotal"] or 0), 2)
    fee = round(float(breakdown["delivery_fee"] or 0), 2)
    down_source = "default_50_percent" if down is None else "custom"
    down_text = (
        f"custom down payment PHP {effective_down:,.2f}" if down is not None
        else f"50% down payment PHP {effective_down:,.2f}"
    )
    updates: OrderUpdates = {"total_amount": total, "required_down_payment": down}
    if instructions:
        updates["payment_instructions"] = instructions
    event = (
        f"Administrator set the payment terms: printing subtotal PHP {subtotal:,.2f}; "
        f"delivery fee PHP {fee:,.2f}; confirmed total PHP {total:,.2f}; {down_text}; "
        f"remaining after down payment PHP {remaining_after:,.2f}."
    )
    if note:
        event += f" Reason: {note}"
    message = (
        f"The shop updated the payment terms for your order. Confirmed total PHP {total:,.2f}; "
        f"{down_text}; remaining after down payment PHP {remaining_after:,.2f}. "
    )
    if instructions:
        message += f"Payment instructions: {instructions}. "
    message += "Review the details in your customer portal."
    notification = notification_services.build(
        order,
        "payment_terms_updated",
        f"payment-terms:{order_repository.order_version(order) + 1}",
        "Payment terms updated",
        message,
    )
    return _save(
        order,
        updates,
        event,
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.terms_set", ADMIN, actor,
            total=total, down_payment=effective_down, down_payment_source=down_source,
            remaining_after_down_payment=remaining_after, payment_instructions_changed=stored_instructions != instructions,
            note=note,
        ),
        notifications=[notification],
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
    projected = {**order, "payments": payments}
    updates: OrderUpdates = {"payments": payments}
    total_paid = payments_total(payments)
    confirmed = order["status"] == "Awaiting Down Payment" and total_paid >= down_payment_due(order) - 0.005
    extra: list[dict[str, Any]] = []
    if confirmed:
        updates["status"] = "Confirmed"
    elif order["status"] in {"Awaiting Full Payment", "Awaiting Final Payment"} and release_ready(projected):
        # The projected order includes this payment, so an unreviewed excess or a pending cancellation cannot
        # put the order into a Ready status that the release actions would immediately refuse.
        status, record, notice = release_updates(projected)
        updates["status"] = status
        updates["fulfillment"] = record
        extra.append(notice)
    return updates, confirmed, extra


def _payment_notification(order: OrderRecord, payment: dict[str, Any], confirmed: bool) -> dict[str, Any]:
    payments = order["payments"] + [payment]
    paid = payments_total(payments)
    total = round(float(order["total_amount"] or 0), 2)
    balance = round(max(0.0, total - paid), 2)
    cod = is_cod(order)
    status = payment_status_from_amounts(
        total, 0.0 if cod else down_payment_due(order), paid,
        cod=cod, excess=pending_excess_amount({"payments": payments}),
    )
    review = (
        f" The excess of PHP {payment['excess_amount']:,.2f} is held for admin review and is not counted as paid."
        if payment.get("requires_admin_review") else ""
    )
    if confirmed:
        down_label = "50% down payment" if order.get("required_down_payment") is None else "down payment"
        return notification_services.build(
            order, "order_confirmed", "order-confirmed", "Down payment verified — order confirmed",
            f"We verified your {down_label} of PHP {paid:,.2f}. Your order {order['reference']} is confirmed and is now "
            f"an active order. Payment status: {status}. Remaining balance: PHP {balance:,.2f}.{review}",
        )
    return notification_services.build(
        order, "payment_verified", f"payment-verified:{payment['id']}", "Payment verified",
        f"We recorded PHP {float(payment['amount']):,.2f} received and credited PHP "
        f"{payment_applied_amount(payment):,.2f}. Total confirmed payments: "
        f"PHP {paid:,.2f}. Payment status: {status}. Remaining balance: PHP {balance:,.2f}.{review}",
    )


def _check_payable(order: OrderRecord, *, allow_excess: bool = False) -> float:
    """Common rules for recording money: the price is final and agreed, and money is due.

    A customer submission can never exceed the remaining balance. An administrator may confirm an amount that
    creates an excess, but only the part up to the remaining balance is credited; the excess is held for review
    and never silently applied as revenue.
    """
    if order["status"] not in PAYABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Payment cannot be recorded at this order stage.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed quotation before recording payment.")
    if not payment_option(order):
        raise HTTPException(status_code=409, detail="The customer must accept the quotation first.")
    pending_excess = pending_excess_amount(order)
    if pending_excess > MONEY_TOLERANCE:
        raise HTTPException(
            status_code=409,
            detail=f"Resolve the flagged excess of PHP {pending_excess:,.2f} before recording another payment.",
        )
    balance = round(float(order["total_amount"]) - paid_amount(order), 2)
    if balance <= 0 and not allow_excess:
        raise HTTPException(status_code=409, detail="This order is already fully paid.")
    # The customer must approve the finished work before the remaining balance can be recorded, even when
    # an administrator is the one entering the payment. Legacy COD orders have no locked portal balance.
    if balance > 0 and not is_cod(order) and paid_amount(order) >= down_payment_due(order) - 0.005:
        ensure_final_payment_allowed(order)
    return balance


def _excess_review(order: Mapping[str, Any], amount: float) -> dict[str, Any]:
    """Split received money into the part credited to the order and the part held for review."""
    total = round(float(order.get("total_amount") or 0), 2)
    balance = round(max(0.0, total - paid_amount(order)), 2)
    applied = round(min(float(amount), balance), 2)
    excess = round(max(0.0, float(amount) - applied), 2)
    requires_review = excess > MONEY_TOLERANCE
    return {
        "applied_amount": applied,
        "excess_amount": excess,
        "requires_admin_review": requires_review,
        "excess_review_status": "pending" if requires_review else "none",
    }


def _excess_suffix(review: Mapping[str, Any]) -> str:
    return (
        f" EXCESS of PHP {review['excess_amount']:,.2f} was held for admin review; only "
        f"PHP {review['applied_amount']:,.2f} was credited."
        if review.get("requires_admin_review") else ""
    )


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
    balance = _check_payable(order, allow_excess=True)
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
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the payment was actually received.")
    method = _payment_method(payload.get("method"))
    reference_text, reference_type = _payment_reference(method, payload.get("reference"))
    note = _text(payload.get("verification_note", "") or "", "Verification note", 300)
    review = _excess_review(order, amount)
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
        **review,
        "verification": {
            "status": "verified",
            "method": "admin_cash_receipt" if method in CASH_METHODS else "admin_confirmed_reference",
            "verified_by": actor,
            "verified_at": now,
            "note": note,
            "admin_review_required": review["requires_admin_review"],
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
        + (f" The order is fully paid and now {updates['status']}." if extra else "")
        + _excess_suffix(review),
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.recorded", ADMIN, actor,
            payment_id=payment["id"], amount=amount, applied_amount=payment["applied_amount"],
            method=method, reference=reference_text, reference_type=reference_type,
            kind=payment["kind"], confirmed=confirmed, note=note,
            excess_amount=review["excess_amount"], requires_admin_review=review["requires_admin_review"],
        ),
        notifications=[_payment_notification(order, payment, confirmed), *extra],
        claim_ops=[{
            "op": "acquire", "kind": "payment", "key": _payment_reference_key(reference_text),
            "detail": "This payment reference has already been recorded. Check the reference number.",
        }],
    )


def resolve_payment_excess(
    reference: str, payment_number: Any, payload: Mapping[str, Any], actor: str = ADMIN
) -> dict[str, Any]:
    """Close an excess-review case without changing the agreed total or the credited payment.

    The money was already received, but it was never credited as revenue. The administrator must state what
    happened to it (returned to the customer or credited to their account) and why. After resolution the order
    can move to Completed; the original payment amount and the audit trail are preserved.
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
    excess = round(float(payment.get("excess_amount") or 0), 2)
    if excess <= MONEY_TOLERANCE or not payment.get("requires_admin_review"):
        raise HTTPException(status_code=409, detail="This payment has no excess to review.")
    if str(payment.get("excess_review_status") or "") == "resolved" or payment.get("excess_resolved_at"):
        raise HTTPException(status_code=409, detail="This excess was already resolved.")
    resolution = _text(payload.get("resolution") or "", "Resolution", 40, True).lower()
    if resolution not in {"returned", "credited"}:
        raise HTTPException(status_code=422, detail="Select returned or credited as the excess resolution.")
    note = _text(payload.get("note") or "", "Resolution note", 500, True)
    now = now_iso()
    payment.update({
        "excess_review_status": "resolved",
        "excess_resolved_at": now,
        "excess_resolved_by": actor,
        "excess_resolution": resolution,
        "excess_resolution_note": note,
    })
    verification = dict(payment.get("verification") or {})
    verification["admin_review_required"] = False
    verification["admin_review_resolved_at"] = now
    verification["admin_review_resolved_by"] = actor
    payment["verification"] = verification
    payments[number - 1] = payment
    return _save(
        order,
        {"payments": payments},
        f"Administrator resolved the PHP {excess:,.2f} excess on payment #{number} as {resolution}: {note}. "
        f"The money received was PHP {float(payment.get('amount') or 0):,.2f}; only PHP "
        f"{payment_applied_amount(payment):,.2f} is credited to the order.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit(
            "payment.excess_resolved", ADMIN, actor,
            payment_number=number, payment_id=payment.get("id", ""), excess_amount=excess,
            resolution=resolution, note=note,
        ),
    )


def _find_submission(order: OrderRecord, submission_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = [dict(item) for item in submissions(order)]
    found = next((item for item in items if item.get("id") == submission_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Payment submission not found.")
    if found.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"This payment submission was already {found.get('status')}.")
    return items, found


def _payment_corrections(submission: Mapping[str, Any], amount: float, method: str, reference: str) -> list[dict[str, Any]]:
    """Describe every admin change to the values OCR read or the customer submitted.

    The audit log keeps the original extraction, the customer's value, and the value actually recorded, so an
    incorrect read can never be silently changed into money or into a different reference.
    """
    ocr = submission.get("ocr") if isinstance(submission.get("ocr"), Mapping) else {}
    fields = ocr.get("fields") if isinstance(ocr.get("fields"), Mapping) else {}
    entries: list[dict[str, Any]] = []

    def differs(field: str, left: Any, right: Any) -> bool:
        if field == "amount":
            try:
                return abs(float(left) - float(right)) > 0.005
            except (TypeError, ValueError):
                return str(left).strip() != str(right).strip()
        if field == "reference":
            return _payment_reference_key(str(left or "")) != _payment_reference_key(str(right or ""))
        return str(left or "").casefold() != str(right or "").casefold()

    for field, submitted, extracted, final in (
        ("amount", submission.get("amount"), fields.get("amount"), amount),
        ("method", submission.get("method"), fields.get("method"), method),
        ("reference", submission.get("reference"), fields.get("reference"), reference),
    ):
        submitted_text = str(submitted if submitted is not None else "").strip()
        extracted_text = str(extracted or "").strip()
        final_text = str(final if final is not None else "").strip()
        changed_from = []
        if extracted_text and differs(field, extracted_text, final_text):
            changed_from.append("ocr")
        if submitted_text and differs(field, submitted_text, final_text):
            changed_from.append("submission")
        if changed_from:
            entries.append({
                "field": field,
                "ocr_value": extracted_text,
                "submitted_value": submitted_text,
                "final_value": final_text,
                "changed_from": changed_from,
            })
    return entries


def verify_payment_submission(reference: str, submission_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Verify a payment the customer submitted. It becomes exactly one payment record and can never be verified twice.

    The admin confirmation form is prefilled from OCR, but every field may be corrected. Corrections are
    applied here (never in the browser), checked against the one-reference-one-payment rule, and written to
    the audit log together with the payment. When it completes the 50% down payment, the order moves to
    Confirmed automatically and the customer is notified once.
    """
    order = _admin_order(reference)
    items, submission = _find_submission(order, submission_id)
    balance = _check_payable(order, allow_excess=True)
    if payload.get("received") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the money was actually received before verifying.")
    amount = payload.get("amount")
    if amount in (None, ""):
        amount = submission["amount"]
    if not _valid_amount(amount):
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter the amount actually received.")
    if any(payment.get("submission_id") == submission_id for payment in order["payments"]):
        raise HTTPException(status_code=409, detail="This payment submission was already recorded.")

    receipt_hash = str(submission.get("receipt_hash") or "")
    _ensure_receipt_unused(receipt_hash)

    submitted_method = str(submission.get("method") or "")
    corrected_method = payload.get("method")
    method = _payment_method(corrected_method) if corrected_method not in (None, "") else submitted_method
    if not method:
        raise HTTPException(status_code=422, detail="Choose the payment method that was received.")
    original_reference = str(submission.get("reference") or "").strip()
    corrected_reference = payload.get("reference")
    reference_text = str(corrected_reference).strip() if corrected_reference not in (None, "") else original_reference
    if method in CASH_METHODS:
        if reference_text:
            reference_type = "official_receipt"
        else:
            reference_text, reference_type = _cash_receipt_number(), "system_cash_receipt"
    else:
        if len(_payment_reference_key(reference_text)) < 4:
            raise HTTPException(status_code=422, detail="Enter the complete payment reference or transaction number.")
        reference_type = "transaction"
    note = _text(payload.get("verification_note", "") or "", "Verification note", 300)
    correction_note = _text(payload.get("correction_note", "") or "", "Correction reason", 300)
    corrections = _payment_corrections(submission, amount, method, reference_text)

    _ensure_reference_unused(
        reference_text,
        ignore_submission_id=submission_id,
        order_reference=str(order.get("reference") or ""),
    )
    claim_ops: list[dict[str, Any]] = []
    if _payment_reference_key(reference_text) != _payment_reference_key(original_reference):
        if original_reference:
            claim_ops.append({"op": "release", "kind": "payment", "key": _payment_reference_key(original_reference)})
        if reference_text and reference_type != "system_cash_receipt":
            claim_ops.append({
                "op": "acquire", "kind": "payment", "key": _payment_reference_key(reference_text),
                "context": submission_id,
                "detail": "This payment reference has already been submitted. Check the reference number.",
            })
    if receipt_hash:
        # Atomic guard: two admins verifying two submissions of the same file cannot both credit money.
        claim_ops.append({
            "op": "acquire", "kind": "receipt", "key": _receipt_file_key(receipt_hash),
            "context": submission_id,
            "detail": "This receipt file was already used for a verified payment. One receipt cannot pay twice.",
        })
    now = now_iso()
    review = _excess_review(order, amount)
    stored_submission_verification = submission.get("verification") if isinstance(submission.get("verification"), dict) else {}
    payment = {
        "id": secrets.token_hex(8),
        "amount": amount,
        "method": method,
        "reference": reference_text,
        "received_at": now,
        "kind": _payment_kind(order, paid_amount(order)),
        "source": "customer submission",
        "submission_id": submission_id,
        "recorded_by": actor,
        "reference_type": reference_type,
        **review,
        "verification": {
            "status": "verified",
            "method": "customer_submission",
            "verified_by": actor,
            "verified_at": now,
            "note": note,
            "admin_review_required": review["requires_admin_review"],
            "ocr_status": stored_submission_verification.get("status", ""),
            "ocr_confidence": (submission.get("ocr") or {}).get("confidence", 0) if isinstance(submission.get("ocr"), dict) else 0,
            "receipt_hash": submission.get("receipt_hash", ""),
            "corrections": corrections,
            "correction_note": correction_note,
        },
        # Historical audit data: the extracted receipt information is kept with the payment record.
        "ocr": submission.get("ocr", {}),
        "receipt_hash": submission.get("receipt_hash", ""),
    }
    stored = submission.get("verification") if isinstance(submission.get("verification"), dict) else {}
    provider_confirmed = str(actor or "") != ADMIN
    stored.update({
        "status": verification_services.VERIFIED,
        "status_label": verification_services.STATUS_LABELS[verification_services.VERIFIED],
        "result": verification_services.RESULT_VERIFIED,
        "result_label": verification_services.RESULT_LABELS[verification_services.RESULT_VERIFIED],
        "verification_method": "provider_api" if provider_confirmed else "manual",
        "verification_basis": "provider_api" if provider_confirmed else "manual_admin",
        "verification_basis_label": (
            "Independently confirmed by the payment provider" if provider_confirmed
            else "Manually approved by an administrator"
        ),
        "reviewed_at": now,
        "reviewed_by": actor,
        "requires_admin": False,
        "auto_confirm": False,
        "corrections": corrections,
    })
    submission.update({
        "status": "verified", "reviewed_at": now, "reviewed_by": actor, "payment_id": payment["id"], "verified_amount": amount,
        "verification_status": verification_services.VERIFIED, "verification": stored,
        "verified_method": method, "verified_reference": reference_text,
    })
    updates, confirmed, extra = _payment_updates(order, payment)
    updates["payment_submissions"] = items
    reported = float(submission["amount"])
    adjusted = f" (customer reported PHP {reported:,.2f})" if abs(amount - reported) > 0.005 else ""
    corrected = f" The OCR/customer details were corrected by the administrator: {', '.join(item['field'] for item in corrections)}." if corrections else ""
    verifier = "an administrator" if actor == ADMIN else str(actor or "the payment provider")
    audits = [_audit(
        "payment.verified", ADMIN, actor,
        submission_id=submission_id, payment_id=payment["id"], amount=amount,
        applied_amount=payment["applied_amount"], reported_amount=reported,
        reference=payment["reference"], method=method, kind=payment["kind"], confirmed=confirmed,
        corrections=corrections, note=note,
        excess_amount=review["excess_amount"], requires_admin_review=review["requires_admin_review"],
    )]
    if corrections:
        audits.insert(0, _audit(
            "payment.ocr_corrected", ADMIN, actor,
            submission_id=submission_id, corrections=corrections, reason=correction_note,
            verification_note=note, reference_from=original_reference, reference_to=reference_text,
        ))
    return _save(
        order,
        updates,
        f"The customer's payment of PHP {amount:,.2f}{adjusted} via {payment['method']} "
        f"(reference {payment['reference']}) was verified by {verifier} and recorded as {payment['kind'].lower()}."
        + corrected
        + (CONFIRMED_SUFFIX if confirmed else "")
        + (f" The order is fully paid and now {updates['status']}." if extra else "")
        + _excess_suffix(review),
        payload.get("expected_version"),
        admin=True,
        audit=audits,
        claim_ops=claim_ops,
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
        "result": verification_services.RESULT_REJECTED,
        "result_label": verification_services.RESULT_LABELS[verification_services.RESULT_REJECTED],
        "verification_basis": "manual_admin",
        "verification_basis_label": "Manually rejected by an administrator",
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
                   "result": verification_services.RESULT_MANUAL_REVIEW,
                   "result_label": verification_services.RESULT_LABELS[verification_services.RESULT_MANUAL_REVIEW],
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


# ------------------------------------------------------------------ finished-work review


def _work_reviewable_statuses(order: Mapping[str, Any]) -> set[str]:
    """Statuses in which the shop can send (or resend) the finished work for customer review."""
    statuses = {"Confirmed", "In Progress", "Approved by Customer"}
    if str(work_approval(order).get("status") or "") == "revision_requested":
        statuses.add("Revision Requested")
    return statuses


@file_services.compensating_files
def send_work_preview(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Upload the finished work and ask the customer to approve it before the final payment unlocks."""
    order = _admin_order(reference)
    ensure_no_pending_cancellation(order)
    if order["status"] not in _work_reviewable_statuses(order):
        raise HTTPException(status_code=409, detail="Finished work can only be sent from an active order waiting for review.")
    if order["total_amount"] is None:
        raise HTTPException(status_code=409, detail="Set the confirmed total before sending finished work.")
    if not down_payment_met(order):
        raise HTTPException(status_code=409, detail="Verify the required down payment before sending finished work.")
    remarks = _text(payload.get("remarks", "") or "", "Preview remarks", 1000)
    metadata, contents = _decode_upload(payload.get("file"), MAX_FILE_BYTES)
    if metadata["mime_type"] not in ocr_services.RECEIPT_MIME_TYPES:
        raise HTTPException(status_code=422, detail="Upload the finished-work preview as a JPG, PNG, or PDF file.")
    try:
        file_services.save(metadata, contents)  # type: ignore[arg-type]
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(status_code=500, detail="The finished-work preview could not be saved.") from error
    now = now_iso()
    previous = work_approval(order)
    history = [dict(item) for item in (previous.get("history") or []) if isinstance(item, dict)]
    if previous.get("status") == "pending":
        history.append({**previous, "status": "superseded", "superseded_at": now})
    record = {
        "status": "pending",
        "preview_files": [metadata],
        "remarks": remarks,
        "sent_at": now,
        "responded_at": None,
        "approval_note": "",
        "revision_note": "",
        "revision_count": int(previous.get("revision_count") or 0),
        "history": history,
    }
    try:
        return _save(
            order,
            {"work_approval": record, "status": "Ready for Customer Review"},
            f"Administrator sent the finished work '{metadata['name']}' for customer review. Final payment stays locked until approval."
            + (f" Remarks: {remarks}" if remarks else ""),
            payload.get("expected_version"),
            admin=True,
            audit=_audit("work.preview_sent", ADMIN, actor, file=metadata["name"], remarks=remarks),
            notifications=[notification_services.build(
                order, "work_ready_for_review", f"work-preview:{metadata['id']}", "Finished work ready for your review",
                f"The shop uploaded the finished work for order {order['reference']}. Review it in your customer portal, then approve it or request a revision. The remaining balance unlocks after approval.",
            )],
        )
    except HTTPException:
        file_services.delete(metadata["filename"])
        raise


def approve_work(reference: str, email: Any, note: Any, customer_id: str | None = None) -> dict[str, Any]:
    """The customer accepts the finished work; only then can the remaining balance be submitted."""
    order = customer_owned_order(reference, email, customer_id)
    record = work_approval(order)
    if str(record.get("status") or "") != "pending":
        raise HTTPException(status_code=409, detail="There is no finished work waiting for your approval.")
    if not record.get("preview_files"):
        raise HTTPException(status_code=409, detail="The finished-work preview is unavailable. Ask the shop to send it again.")
    ensure_no_pending_cancellation(order)
    approval_note = _text(note, "Approval note", 1000)
    now = now_iso()
    record.update({
        "status": "approved",
        "approved_at": now,
        "responded_at": now,
        "approval_note": approval_note,
    })
    return _save(
        order,
        {"work_approval": record, "status": "Awaiting Final Payment"},
        f"Customer approved the finished work. The remaining balance is now unlocked."
        + (f" Note: {approval_note}" if approval_note else ""),
        audit=_audit("work.approved", CUSTOMER, customer_id, note=approval_note),
        notifications=[notification_services.build(
            order, "work_approved", f"work-approved:{now}", "Finished work approved",
            f"You approved the finished work for order {order['reference']}. The remaining balance is now available in your customer portal.",
        )],
    )


def request_work_revision(reference: str, email: Any, note: Any, customer_id: str | None = None) -> dict[str, Any]:
    """The customer asks for changes to the finished work; final payment stays locked."""
    order = customer_owned_order(reference, email, customer_id)
    record = work_approval(order)
    if str(record.get("status") or "") != "pending":
        raise HTTPException(status_code=409, detail="There is no finished work waiting for your review.")
    revision_note = _text(note, "Requested changes", 1000, True)
    ensure_no_pending_cancellation(order)
    now = now_iso()
    record.update({
        "status": "revision_requested",
        "revision_note": revision_note,
        "responded_at": now,
        "revision_count": int(record.get("revision_count") or 0) + 1,
    })
    return _save(
        order,
        {"work_approval": record, "status": "Revision Requested"},
        f"Customer requested a revision of the finished work. Final payment remains locked. Reason: {revision_note}",
        audit=_audit("work.revision_requested", CUSTOMER, customer_id, note=revision_note),
        notifications=[notification_services.build(
            order, "work_revision_requested", f"work-revision:{now}", "Revision requested",
            f"You requested changes to the finished work for order {order['reference']}: {revision_note}. "
            "The shop will send an updated preview; the remaining balance stays locked until you approve it.",
        )],
    )


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
    # New review-first workflow. Sending a preview and approving/revising it have their own actions, so the
    # direct status moves here are only the ones an administrator can safely make by hand.
    "In Progress": {"Ready for Customer Review", "Rejected", "Cancelled"},
    "Revision Requested": {"Ready for Customer Review", "In Progress", "Rejected", "Cancelled"},
    "Approved by Customer": {"Awaiting Final Payment", "In Progress", "Rejected", "Cancelled"},
    "Awaiting Final Payment": {"Rejected", "Cancelled"},
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
    "Completed": ("Order completed", "Your order is completed. Thank you for ordering with ImPRESS Print Works!"),
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
    if next_status == "Completed":
        pending_excess = pending_excess_amount(order)
        if pending_excess > MONEY_TOLERANCE:
            raise HTTPException(
                status_code=409,
                detail=f"Resolve the flagged excess of PHP {pending_excess:,.2f} before marking this order as completed.",
            )
        if not is_fully_paid(order):
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
    due = next_customer_payment(order)
    review_pending = str(work_approval(order).get("status") or "") == "pending"
    return {
        "accept_quotation": status == "Quotation Sent" and quote.get("status") == "sent",
        "decline_quotation": status == "Quotation Sent" and quote.get("status") == "sent",
        "pay": due is not None and not due.get("locked") and not pending_submissions(order) and not pending_cancellation(order),
        "approve_draft": status == "Awaiting Customer Approval" and bool(order["draft_attachment"]),
        "request_revision": status == "Awaiting Customer Approval" and bool(order["draft_attachment"]),
        "approve_work": review_pending,
        "request_work_revision": review_pending,
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

