"""Admin analytics, reports, and table queries built from the real order and customer records.

Every figure here is calculated from the database on each request; nothing is stored or estimated.

Business rules (shared by the dashboard, the reports, the customer table, and the exports):

* **Confirmed revenue** counts an order only when it is ``Completed`` *and* fully paid (recorded
  payments cover the confirmed total). Each order is counted once, for its confirmed total, on the
  date it was completed. Pending, partially paid, unpaid, cancelled, and rejected orders never count.
* **Customer amount spent** uses the same rule, so a customer's spend always adds up to revenue.
* **Completion duration** is the number of calendar days (weekends included) between the shop-local
  request date and the shop-local completion date, from the stored timestamps.
* Dates are shown and filtered on the shop's calendar (Philippine time, UTC+8).
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from backend.app.models.order import FULFILLMENT_METHODS, PAYMENT_OPTIONS, OrderRecord
from backend.app.repositories import admin_repository, catalog_repository, chatbot_repository, customer_repository, order_repository
from backend.app.services import fulfillment_services, order_services, payment_verification_services, refund_services, shop_settings_services
from backend.app.utils.order_number import SHOP_TIMEZONE


PENDING_STATUSES = order_services.PENDING_STATUSES
PROCESSING_STATUSES = order_services.PROCESSING_STATUSES
CANCELLED_STATUSES = order_services.CANCELLED_STATUSES
STATUS_ORDER = list(order_services.ORDER_STATUSES)
STATUS_GROUPS = {
    "pending": PENDING_STATUSES,
    "processing": PROCESSING_STATUSES,
    "completed": {"Completed"},
    "cancelled": CANCELLED_STATUSES,
}
PAYMENT_STATUSES = ("Fully Paid", "Partially Paid", "Unpaid", "Not Quoted")
PAGE_SIZES = (10, 25, 50, 100)
OTHER_SERVICE = {"id": "other", "label": "Other / unassigned"}
# Older orders saved only the category label. These words map them to the seeded service ids.
SERVICE_KEYWORDS = (
    ("invitation", "invitation"),
    ("souvenir", "souvenir"),
    ("sticker", "sticker"),
    ("sintra", "sintra-board"),
    ("party", "party-needs"),
    ("tarpaulin", "large-format"),
    ("large format", "large-format"),
    ("t-shirt", "tshirt-printing"),
    ("tshirt", "tshirt-printing"),
)


# ================================================================== small helpers


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_date(value: Any) -> date | None:
    """The shop-calendar (UTC+8) date of a stored UTC timestamp."""
    parsed = parse_timestamp(value)
    return parsed.astimezone(SHOP_TIMEZONE).date() if parsed else None


def today_local() -> date:
    return datetime.now(SHOP_TIMEZONE).date()


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def param_date(params: Mapping[str, Any], key: str, label: str) -> date | None:
    value = str(params.get(key) or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise bad_request(f"{label} must be a valid date (YYYY-MM-DD).") from error


def param_int(params: Mapping[str, Any], key: str, label: str, low: int = 0, high: int = 100000) -> int | None:
    value = str(params.get(key) or "").strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError as error:
        raise bad_request(f"{label} must be a whole number.") from error
    if not low <= number <= high:
        raise bad_request(f"{label} must be between {low} and {high}.")
    return number


def param_text(params: Mapping[str, Any], key: str, limit: int = 120) -> str:
    return str(params.get(key) or "").strip()[:limit]


def date_range(params: Mapping[str, Any], start_key: str, end_key: str, label: str) -> tuple[date | None, date | None]:
    start = param_date(params, start_key, f"{label} start")
    end = param_date(params, end_key, f"{label} end")
    if start and end and start > end:
        raise bad_request(f"The {label.lower()} start date must be on or before the end date.")
    return start, end


def in_range(value: date | None, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    if value is None:
        return False
    return (start is None or value >= start) and (end is None or value <= end)


def money(value: float | None) -> float:
    return round(float(value or 0), 2)


# ================================================================== services (categories)


class ServiceIndex:
    """Maps each order to its printing service (catalog category)."""

    def __init__(self) -> None:
        self.services = [{"id": item["id"], "label": item["label"]} for item in catalog_repository.list_categories()]
        self.by_id = {item["id"]: item for item in self.services}
        self.by_label = {item["label"].strip().lower(): item for item in self.services}
        self.extra: dict[str, dict[str, str]] = {}

    def resolve(self, order: Mapping[str, Any]) -> dict[str, str]:
        product = order.get("product") or {}
        category_id = product.get("category_id")
        if category_id and category_id in self.by_id:
            return self.by_id[category_id]
        label = str(product.get("category") or "").strip()
        lowered = label.lower()
        if lowered in self.by_label:
            return self.by_label[lowered]
        for keyword, service_id in SERVICE_KEYWORDS:
            if keyword in lowered and service_id in self.by_id:
                return self.by_id[service_id]
        if not label:
            return OTHER_SERVICE
        # A category that no longer exists in the catalog keeps its own name in reports.
        key = f"legacy-{'-'.join(lowered.split())}"
        return self.extra.setdefault(key, {"id": key, "label": label})

    def all(self) -> list[dict[str, str]]:
        return self.services + list(self.extra.values())



def service_label(service_id: str) -> str:
    """Label of a catalog service or of a legacy service that appears on saved orders."""
    orders, services = _load()
    options = service_options(services, (order_row(order, services) for order in orders))
    found = next((item for item in options if item["id"] == service_id), None)
    if found is None:
        if service_id == OTHER_SERVICE["id"]:
            return OTHER_SERVICE["label"]
        raise bad_request("Select a valid service.")
    return found["label"]


# ================================================================== per-order calculations


paid_amount = order_services.paid_amount
is_fully_paid = order_services.is_fully_paid


def payment_status(order: Mapping[str, Any]) -> str:
    if order["total_amount"] is None:
        return "Not Quoted"
    if is_fully_paid(order):
        return "Fully Paid"
    if paid_amount(order) > 0:
        return "Partially Paid"
    return "Unpaid"


def is_confirmed_revenue(order: Mapping[str, Any]) -> bool:
    """Completed and fully paid. A completed order being reprinted keeps its original recognized revenue."""
    if order["status"] == "Completed":
        return is_fully_paid(order)
    basis = order_services.revenue_basis(order)
    return basis is not None and float(basis) > 0 and paid_amount(order) >= round(float(basis), 2) - 0.005


def confirmed_revenue(order: Mapping[str, Any]) -> float:
    """The order's confirmed total, counted once, only for completed and fully paid orders."""
    return money(order_services.revenue_basis(order)) if is_confirmed_revenue(order) else 0.0


def completion_date(order: Mapping[str, Any]) -> date | None:
    if not order_services.counts_as_completed(order):
        return None
    return local_date(order.get("completed_at"))


def completion_days(order: Mapping[str, Any]) -> int | None:
    """Calendar days from the request date to the completion date (weekends included)."""
    requested = local_date(order.get("created_at"))
    completed = completion_date(order)
    if requested is None or completed is None:
        return None
    days = (completed - requested).days
    return days if days >= 0 else None


def description(order: Mapping[str, Any], limit: int = 3) -> str:
    customization = order.get("customization") or {}
    product = order.get("product") or {}
    text = f"{product.get('name', 'Order')} × {customization.get('quantity', 1)}"
    specs = customization.get("specifications")
    details: list[str] = []
    if isinstance(specs, list):
        details = [f"{spec.get('label')}: {spec.get('value')}" for spec in specs if spec.get("value")]
    else:
        details = [str(customization[key]) for key in ("size", "material") if customization.get(key)]
    if details:
        shown = details[:limit]
        more = len(details) - len(shown)
        text += " — " + "; ".join(shown) + (f" (+{more} more)" if more > 0 else "")
    return text


def order_row(order: OrderRecord, services: ServiceIndex) -> dict[str, Any]:
    """The flat record used by every admin table, report, and export."""
    service = services.resolve(order)
    paid = paid_amount(order)
    total = order["total_amount"]
    estimate = (order.get("pricing") or {}).get("estimated_total")
    images = [item for item in order["attachments"] if str(item.get("mime_type", "")).startswith("image/")]
    customization = order.get("customization") or {}
    package = customization.get("package") if isinstance(customization.get("package"), dict) else {}
    breakdown = order_services.payment_breakdown(order)
    return {
        "reference": order["reference"],
        "customer_id": order.get("customer_id"),
        "customer_name": order["customer"].get("name", ""),
        "customer_email": order["customer"].get("email", ""),
        "customer_phone": order["customer"].get("phone", ""),
        "service_id": service["id"],
        "service": service["label"],
        "product": (order.get("product") or {}).get("name", ""),
        "product_id": (order.get("product") or {}).get("id", ""),
        "product_image": (order.get("product") or {}).get("image", ""),
        "order_kind": customization.get("order_kind", ""),
        "package_name": package.get("name", ""),
        "design_name": (customization.get("design") or {}).get("name", "") if isinstance(customization.get("design"), dict) else "",
        "description": description(order),
        "quantity": (order.get("customization") or {}).get("quantity"),
        "created_at": order["created_at"],
        "request_date": iso(local_date(order["created_at"])),
        "status": order["status"],
        "total_amount": money(total) if total is not None else None,
        "estimated_total": estimate if isinstance(estimate, (int, float)) else None,
        "amount_paid": paid,
        "balance": money(total - paid) if total is not None else None,
        "payment_status": payment_status(order),
        "payment_option": breakdown["payment_option"],
        "payment_option_label": breakdown["payment_option_label"],
        "down_payment_required": breakdown["down_payment_required"],
        "down_payment_status": breakdown["down_payment_status"],
        "payment_count": len(order["payments"]),
        "pending_verification": breakdown["pending_verification"],
        "payments_to_verify": len(order_services.pending_submissions(order)),
        "fulfillment_method": order_services.fulfillment_method(order),
        "fulfillment_label": FULFILLMENT_METHODS[order_services.fulfillment_method(order)],
        "quotation_status": order_services.quotation(order).get("status", ""),
        "fixed_price": order_services.is_fixed_price(order),
        "price_basis": order_services.price_basis(order),
        "standard_size": ((customization.get("price_list") or {}).get("size", "") if isinstance(customization.get("price_list"), dict) else ""),
        "price_agreed": order_services.price_agreed(order),
        "completed_at": order.get("completed_at") if order_services.counts_as_completed(order) else None,
        "reported_completed": order_services.counts_as_completed(order),
        "reprint_in_progress": order_services.active_reprint(order) is not None,
        "completion_date": iso(completion_date(order)),
        "completion_days": completion_days(order),
        "confirmed_revenue": confirmed_revenue(order),
        "attachment_count": len(order["attachments"]),
        "image_count": len(images),
        "updated_at": order["updated_at"],
    }


def admin_order_view(order: OrderRecord, services: ServiceIndex | None = None) -> dict[str, Any]:
    """Full order details for the admin modal, plus the calculated fields and the allowed actions."""
    services = services or ServiceIndex()
    view = order_services.order_view(order, admin=True)
    row = order_row(order, services)
    status = order["status"]
    total = order["total_amount"]
    paid = row["amount_paid"]
    view["summary"] = row
    view["customer_id"] = order.get("customer_id")
    view["allowed_statuses"] = order_services.allowed_transitions(order)
    pending = order_services.pending_submissions(order)
    cancellation = order_services.pending_cancellation(order)
    method = order_services.fulfillment_method(order)
    chosen = bool(order_services.payment_option(order))
    payable = status in order_services.PAYABLE_STATUSES and total is not None and chosen and paid < total - 0.005
    view["actions"] = {
        "quote": order_services.quotable(order),
        # Approving a package order requests the down payment; only a verified down payment confirms the order.
        "confirm_package": order_services.is_fixed_price(order) and status in {"Pending Confirmation", "Under Review"},
        "confirm_cod": False,
        "verify_payments": payable and bool(pending),
        "record_payment": payable and not pending,
        "send_draft": status in order_services.DRAFTABLE_STATUSES and order_services.down_payment_met(order),
        "start_production_blocked": status == "Approved for Printing" and not order_services.down_payment_met(order),
        "complete_requires_payment": status in order_services.RELEASE_STATUSES and not is_fully_paid(order),
        "cancel_blocked_by_payments": bool(order["payments"]) or bool(pending),
        "change_payment_option": order_services.payment_option_changeable(order) and status not in CANCELLED_STATUSES,
        # Production monitoring, quality checking, packaging, and fulfillment (fulfillment_services).
        "update_production": status in {"Approved for Printing", "In Production", "Quality Checking", "Packaging"} and not cancellation,
        "quality_check": status == "Quality Checking" and not cancellation,
        "complete_packaging": status == "Packaging" and not cancellation,
        "dispatch": status == "Ready for Dispatch" and not cancellation,
        "confirm_delivery": status == "Out for Delivery" and not cancellation,
        "delivery_failed": status == "Out for Delivery",
        "reschedule_delivery": status == "Delivery Failed" and not cancellation,
        "schedule_pickup": method == "pickup" and status in {"Packaging", "Awaiting Full Payment", "Ready for Pickup"},
        "confirm_pickup": status == "Ready for Pickup" and not cancellation,
        "awaiting_full_payment": status == "Awaiting Full Payment",
        # Cancellation requests and refunds (refund_services).
        "cancellation_pending": bool(cancellation),
        "create_refund": refund_services.max_refundable(order) > 0,
        "start_reprint": status in order_services.REPRINTABLE_STATUSES and not cancellation and order_services.active_reprint(order) is None,
    }
    view["payment_methods"] = [
        method for method in order_services.admin_payment_methods()
        if method != "Cash on Delivery" or order_services.is_cod(order)
    ]
    settings = shop_settings_services.fulfillment_settings()
    view["couriers"] = [{"id": item["id"], "name": item["name"], "service": item["service"]} for item in settings["couriers"] if item["active"]]
    view["default_courier"] = settings["default_courier"]
    view["max_refundable"] = refund_services.max_refundable(order)
    view["cancellation_policy"] = refund_services.policy_guidance(order)
    view["failure_reasons"] = fulfillment_services.FAILURE_REASONS
    view["quality_checks"] = [{"key": key, "label": label} for key, label in fulfillment_services.QUALITY_CHECKS]
    return view


# ================================================================== table helpers


def _sort_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.casefold()
    return value


def sort_rows(rows: list[dict[str, Any]], params: Mapping[str, Any], allowed: Mapping[str, str], default: str, default_dir: str = "desc") -> list[dict[str, Any]]:
    key = str(params.get("sort") or default)
    if key not in allowed:
        key = default
    direction = str(params.get("dir") or default_dir).lower()
    reverse = direction == "desc"
    column = allowed[key]
    present = [row for row in rows if row.get(column) is not None and row.get(column) != ""]
    missing = [row for row in rows if row.get(column) is None or row.get(column) == ""]
    present.sort(key=lambda row: _sort_value(row[column]), reverse=reverse)
    # Rows without a value always go last, whichever direction is chosen.
    return present + missing


def paginate(rows: list[dict[str, Any]], params: Mapping[str, Any]) -> dict[str, Any]:
    total = len(rows)
    if str(params.get("page_size") or "") == "all":
        return {"items": rows, "total": total, "page": 1, "page_size": total, "pages": 1}
    try:
        size = int(params.get("page_size") or 25)
    except ValueError:
        size = 25
    size = size if size in PAGE_SIZES else 25
    pages = max(1, math.ceil(total / size))
    try:
        page = int(params.get("page") or 1)
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    start = (page - 1) * size
    return {"items": rows[start:start + size], "total": total, "page": page, "page_size": size, "pages": pages}


def matches(needle: str, *values: Any) -> bool:
    if not needle:
        return True
    needle = needle.casefold()
    return any(needle in str(value or "").casefold() for value in values)


def _load() -> tuple[list[OrderRecord], ServiceIndex]:
    return order_repository.list_orders(), ServiceIndex()


def service_options(services: ServiceIndex, rows: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, str]]:
    """Catalog services plus any legacy/other service that actually appears in the rows."""
    options = list(services.all())
    seen = {item["id"] for item in options}
    for row in rows:
        if row["service_id"] not in seen:
            options.append({"id": row["service_id"], "label": row["service"]})
            seen.add(row["service_id"])
    return options


def product_options(services: ServiceIndex, orders: Iterable[OrderRecord]) -> list[dict[str, str]]:
    """Every catalog product (printing service) plus products that only appear on saved orders, by service."""
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for category in catalog_repository.load_catalog(active_only=False):
        for item in category["products"]:
            options.append({"id": item["id"], "label": item["name"], "service_id": category["id"]})
            seen.add(item["id"])
    for order in orders:
        product = order.get("product") or {}
        product_id = product.get("id")
        if product_id and product_id not in seen:
            options.append({"id": product_id, "label": product.get("name") or product_id, "service_id": services.resolve(order)["id"]})
            seen.add(product_id)
    return options


# ================================================================== orders table


ORDER_SORTS = {
    "reference": "reference",
    "customer": "customer_name",
    "service": "service",
    "product": "product",
    "request_date": "created_at",
    "total": "total_amount",
    "paid": "amount_paid",
    "status": "status",
    "payment_status": "payment_status",
    "payment_option": "payment_option",
}


def order_rows(params: Mapping[str, Any], orders: list[OrderRecord] | None = None, services: ServiceIndex | None = None) -> tuple[list[dict[str, Any]], ServiceIndex]:
    if orders is None or services is None:
        orders, services = _load()
    search = param_text(params, "search")
    status = param_text(params, "status", 60)
    service = param_text(params, "service", 80)
    product = param_text(params, "product", 120)
    pay = param_text(params, "payment_status", 40)
    kind = param_text(params, "kind", 20)
    option = param_text(params, "payment_option", 20)
    method = param_text(params, "fulfillment", 20)
    customer = param_text(params, "customer", 64)
    if method and method not in FULFILLMENT_METHODS:
        raise bad_request("Select Store Pickup or Delivery.")
    if kind and kind not in {"package", "customize", "design"}:
        raise bad_request("Select a valid order type.")
    if option and option not in PAYMENT_OPTIONS:
        raise bad_request("Select a valid payment option.")
    start, end = date_range(params, "date_from", "date_to", "Request date")
    rows = []
    for order in orders:
        row = order_row(order, services)
        if kind and row["order_kind"] != kind:
            continue
        if option and row["payment_option"] != option:
            continue
        if method and row["fulfillment_method"] != method:
            continue
        if customer and row["customer_id"] != customer:
            continue
        if status:
            group = STATUS_GROUPS.get(status)
            if (group is not None and row["status"] not in group) or (group is None and row["status"] != status):
                continue
        if service and row["service_id"] != service:
            continue
        if product and row["product_id"] != product:
            continue
        if pay and row["payment_status"] != pay:
            continue
        if not in_range(local_date(row["created_at"]), start, end):
            continue
        if not matches(search, row["reference"], row["customer_name"], row["customer_email"], row["product"], row["service"], row["standard_size"]):
            continue
        rows.append(row)
    return sort_rows(rows, params, ORDER_SORTS, "request_date"), services


def order_list(params: Mapping[str, Any]) -> dict[str, Any]:
    orders, services = _load()
    rows, services = order_rows(params, orders, services)
    result = paginate(rows, params)
    result["services"] = service_options(services, (order_row(order, services) for order in orders))
    result["products"] = product_options(services, orders)
    result["statuses"] = STATUS_ORDER
    result["status_counts"] = {status: sum(1 for order in orders if order["status"] == status) for status in STATUS_ORDER}
    return result


# ================================================================== payments table


PAYMENT_SORTS = {
    "received": "received_at",
    "reference": "reference",
    "customer": "customer_name",
    "service": "service",
    "method": "method",
    "amount": "amount",
    "payment_status": "order_payment_status",
    "verification": "verification_status",
    "payment_reference": "payment_reference",
}
# Verification filter values -> the status shown in the table.
VERIFICATION_FILTERS = {
    "verified": order_services.VERIFICATION_VERIFIED,
    "pending": order_services.VERIFICATION_PENDING,
    "rejected": order_services.VERIFICATION_REJECTED,
    "missing": order_services.VERIFICATION_MISSING,
}


def _transaction_entries(order: OrderRecord, base: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every payment entry of one order: recorded (verified) payments, then customer submissions still
    pending or rejected. A verified submission is represented only by the payment it became, so no
    payment is listed twice. Only recorded payments count as paid (``counts_as_paid``)."""
    shared = {
        "reference": order["reference"],
        "customer_id": order.get("customer_id"),
        "customer_name": base["customer_name"],
        "customer_email": base["customer_email"],
        "service_id": base["service_id"],
        "service": base["service"],
        "product": base["product"],
        "order_status": base["status"],
        "order_total": base["total_amount"],
        "order_paid": base["amount_paid"],
        "order_balance": base["balance"],
        "order_payment_status": base["payment_status"],
        "payment_option": base["payment_option"],
        "payment_option_label": base["payment_option_label"],
    }
    entries = []
    submissions_by_id = {str(item.get("id")): item for item in order_services.submissions(order)}

    def ocr_columns(item: dict[str, Any], required: float, verification: dict[str, Any]) -> dict[str, Any]:
        ocr = item.get("ocr") if isinstance(item.get("ocr"), dict) else {}
        fields = ocr.get("fields", {}) if isinstance(ocr, dict) else {}
        stored = item.get("verification") if isinstance(item.get("verification"), dict) else verification
        try:
            extracted = float(str(fields.get("amount") or "").replace(",", ""))
        except ValueError:
            extracted = None
        return {
            "ocr_reference": str(fields.get("reference") or ""),
            "ocr_amount": money(extracted) if extracted is not None else None,
            "required_amount": money(required),
            "ocr_confidence": float(ocr.get("confidence") or 0) if isinstance(ocr, dict) else 0.0,
            "ocr_verification_status": str(stored.get("status") or verification.get("status") or ""),
            "verification_status_label": stored.get("status_label")
            or payment_verification_services.STATUS_LABELS.get(stored.get("status") or verification.get("status") or "", ""),
            "submitted_at": item.get("submitted_at"),
            "receipt_url": (f"/api/admin/orders/{order['reference']}/files/{(item.get('proof') or {}).get('id')}"
                            if isinstance(item.get("proof"), dict) and (item.get("proof") or {}).get("id") else ""),
            "verification_method": item.get("verification_method") or stored.get("verification_method", ""),
        }

    for index, payment in enumerate(order["payments"], start=1):
        verification = order_services.payment_verification(payment)
        linked = submissions_by_id.get(str(payment.get("submission_id") or ""), {})
        entries.append({
            **shared,
            "id": f"{order['reference']}-P{index}",
            "entry_type": "payment",
            "payment_number": index,
            "submission_id": None,
            "amount": money(payment.get("amount")),
            "method": payment.get("method", ""),
            "payment_reference": str(payment.get("reference") or ""),
            "reference_type_label": verification["reference_type_label"],
            "received_at": payment.get("received_at"),
            "received_date": iso(local_date(payment.get("received_at"))),
            "kind": payment.get("kind", ""),
            "verification_status": verification["status"],
            "verification_detail": verification["detail"],
            "verified_by": verification["verified_by"],
            "verified_at": verification["verified_at"],
            "counts_as_paid": True,
            **ocr_columns(linked, float(payment.get("amount") or 0), verification),
        })
    for item in order_services.submissions(order):
        status = item.get("status", "pending")
        if status == "verified":
            continue
        rejected = status == "rejected"
        entries.append({
            **shared,
            "id": f"{order['reference']}-S{item.get('id', '')}",
            "entry_type": "submission",
            "payment_number": None,
            "submission_id": item.get("id"),
            "amount": money(item.get("amount")),
            "method": item.get("method", ""),
            "payment_reference": str(item.get("reference") or ""),
            "reference_type_label": "Customer-submitted reference",
            "received_at": item.get("submitted_at"),
            "received_date": iso(local_date(item.get("submitted_at"))),
            "kind": {"down_payment": "Down payment", "balance": "Balance payment"}.get(item.get("stage", ""), "Payment"),
            "verification_status": order_services.VERIFICATION_REJECTED if rejected else order_services.VERIFICATION_PENDING,
            "verification_detail": (f"Rejected: {item.get('review_note', '')}" if rejected
                                    else "Submitted by the customer with a receipt; not counted as paid until verified"),
            "verified_by": item.get("reviewed_by", "") if rejected else "",
            "verified_at": item.get("reviewed_at") if rejected else None,
            "counts_as_paid": False,
            **ocr_columns(item, float(item.get("amount") or 0), {"status": order_services.VERIFICATION_REJECTED if rejected else order_services.VERIFICATION_PENDING}),
        })
    return entries


def payment_rows(params: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[OrderRecord], ServiceIndex]:
    orders, services = _load()
    search = param_text(params, "search")
    method = param_text(params, "method", 60)
    service = param_text(params, "service", 80)
    pay = param_text(params, "payment_status", 40)
    option = param_text(params, "payment_option", 20)
    verification = param_text(params, "verification", 20)
    if option and option not in PAYMENT_OPTIONS:
        raise bad_request("Select a valid payment option.")
    if verification and verification not in VERIFICATION_FILTERS:
        raise bad_request("Select a valid verification status.")
    start, end = date_range(params, "date_from", "date_to", "Payment date")
    rows = []
    for order in orders:
        base = order_row(order, services)
        if option and base["payment_option"] != option:
            continue
        for row in _transaction_entries(order, base):
            if verification and row["verification_status"] != VERIFICATION_FILTERS[verification]:
                continue
            if method and row["method"] != method:
                continue
            if service and row["service_id"] != service:
                continue
            if pay and row["order_payment_status"] != pay:
                continue
            if not in_range(local_date(row["received_at"]), start, end):
                continue
            if not matches(search, row["reference"], row["customer_name"], row["customer_email"], row["payment_reference"], row["product"]):
                continue
            rows.append(row)
    return sort_rows(rows, params, PAYMENT_SORTS, "received"), orders, services


def payment_list(params: Mapping[str, Any]) -> dict[str, Any]:
    rows, orders, services = payment_rows(params)
    result = paginate(rows, params)
    active = [order for order in orders if order["status"] not in CANCELLED_STATUSES]
    verified_rows = [row for row in rows if row["counts_as_paid"]]
    all_payments = [payment for order in orders for payment in order["payments"]]
    result["summary"] = {
        # Only recorded (verified) payments are money received; pending and rejected submissions are listed separately.
        "filtered_amount": money(sum(row["amount"] for row in verified_rows)),
        "filtered_count": len(verified_rows),
        "filtered_entries": len(rows),
        "filtered_pending_amount": money(sum(row["amount"] for row in rows if row["verification_status"] == order_services.VERIFICATION_PENDING)),
        "missing_reference": sum(1 for payment in all_payments if not str(payment.get("reference") or "").strip()),
        "partially_paid_orders": sum(1 for order in active if payment_status(order) == "Partially Paid"),
        "total_received": money(sum(paid_amount(order) for order in orders)),
        "outstanding_balance": money(sum(
            max(0.0, float(order["total_amount"]) - paid_amount(order))
            for order in active if order_services.price_agreed(order)
        )),
        "fully_paid_orders": sum(1 for order in active if is_fully_paid(order)),
        "awaiting_quotation": sum(1 for order in active if not order_services.price_agreed(order) and order["status"] != "Completed"),
        "awaiting_down_payment": sum(1 for order in active if order["status"] == "Awaiting Down Payment"),
        "awaiting_cod_confirmation": sum(1 for order in active if order["status"] == "Awaiting COD Confirmation"),
        "payments_to_verify": [
            {
                "reference": order["reference"],
                "customer_name": order["customer"].get("name", ""),
                "submission_id": item["id"],
                "amount": money(item.get("amount")),
                "method": item.get("method", ""),
                "payment_reference": item.get("reference", ""),
                "submitted_at": item.get("submitted_at"),
                "has_proof": bool(item.get("proof")),
            }
            for order in active
            for item in order_services.pending_submissions(order)
        ],
        "down_payments_due": money(sum(
            order_services.payment_breakdown(order)["down_payment_remaining"] or 0
            for order in active if order["status"] == "Awaiting Down Payment"
        )),
        "cod_orders": sum(1 for order in active if order_services.is_cod(order) and order["status"] != "Completed"),
        "cod_due": money(sum(
            max(0.0, float(order["total_amount"]) - paid_amount(order))
            for order in active
            if order_services.is_cod(order) and order_services.price_agreed(order) and order["status"] != "Completed"
        )),
        "orders_with_balance": [
            {
                "reference": order["reference"],
                "customer_name": order["customer"].get("name", ""),
                "product": (order.get("product") or {}).get("name", ""),
                "balance": money(float(order["total_amount"]) - paid_amount(order)),
                "status": order["status"],
                "payment_option_label": order_services.payment_option_label(order),
            }
            for order in active
            if order["total_amount"] is not None
            and order["status"] in order_services.PAYABLE_STATUSES
            and float(order["total_amount"]) - paid_amount(order) > 0.005
        ],
    }
    result["methods"] = sorted({payment.get("method", "") for order in orders for payment in order["payments"]} - {""})
    result["services"] = service_options(services, (order_row(order, services) for order in orders))
    return result


# ================================================================== customers


CUSTOMER_SORTS = {
    "name": "name",
    "email": "email",
    "registered": "created_at",
    "orders": "total_orders",
    "completed": "completed_orders",
    "pending": "pending_orders",
    "spent": "total_spent",
    "transactions": "total_transaction_amount",
    "paid": "total_paid",
    "balance": "outstanding_balance",
    "payment_status": "payment_status",
    "last_order": "last_order_at",
}
CUSTOMER_ACTIVITY = {"with_orders", "no_orders", "with_pending", "with_completed", "repeat", "with_spend", "no_spend"}
# A customer's overall payment standing across their non-cancelled orders.
CUSTOMER_PAYMENT_STATUSES = ("With balance", "Fully paid", "Awaiting quotation", "No transactions")
# Customer Management views. Each customer is in at most one of "paid" and "unpaid", decided from the
# recorded payments of their agreed (accepted or approved) orders, so switching views never duplicates rows.
CUSTOMER_VIEWS = {
    "all": None,
    "paid": "Fully paid",  # every agreed order is fully paid
    "unpaid": "With balance",  # at least one agreed order still has a balance
}


def customer_payment_status(active: list[OrderRecord], outstanding: float) -> str:
    if not active:
        return "No transactions"
    if outstanding > 0.005:
        return "With balance"
    if not any(order_services.price_agreed(order) for order in active):
        return "Awaiting quotation"
    return "Fully paid"


def display_customer_id(customer_id: str) -> str:
    return f"CUS-{customer_id.replace('-', '')[:8].upper()}"


def _orders_by_customer(orders: list[OrderRecord], customers: list[dict[str, Any]]) -> dict[str, list[OrderRecord]]:
    """Group orders by account. Older orders without an account id are matched by email address."""
    by_email = {customer["email"].lower(): customer["id"] for customer in customers}
    grouped: dict[str, list[OrderRecord]] = {customer["id"]: [] for customer in customers}
    for order in orders:
        owner = order.get("customer_id") or by_email.get(str(order["customer"].get("email", "")).lower())
        if owner in grouped:
            grouped[owner].append(order)
    return grouped


def customer_stats(customer: Mapping[str, Any], orders: list[OrderRecord]) -> dict[str, Any]:
    active = [order for order in orders if order["status"] not in CANCELLED_STATUSES]
    # Only agreed prices count: an unaccepted quotation or an unconfirmed package order is not owed yet.
    quoted = [order for order in active if order_services.price_agreed(order)]
    outstanding = money(sum(max(0.0, float(order["total_amount"]) - paid_amount(order)) for order in quoted))
    return {
        "id": customer["id"],
        "display_id": display_customer_id(customer["id"]),
        "name": customer["name"],
        "email": customer["email"],
        "phone": customer.get("phone", ""),
        "messenger": customer.get("messenger", ""),
        "created_at": customer["created_at"],
        "registered_date": iso(local_date(customer["created_at"])),
        "total_orders": len(orders),
        "completed_orders": sum(1 for order in orders if order["status"] == "Completed"),
        # Pending means still open: not yet completed and not cancelled or rejected.
        "pending_orders": sum(1 for order in orders if order["status"] in PENDING_STATUSES | PROCESSING_STATUSES),
        "cancelled_orders": sum(1 for order in orders if order["status"] in CANCELLED_STATUSES),
        "total_spent": money(sum(confirmed_revenue(order) for order in orders)),
        "total_paid": money(sum(paid_amount(order) for order in orders)),
        # Confirmed totals of every order that is not cancelled or rejected (completed or still open).
        "total_transaction_amount": money(sum(float(order["total_amount"]) for order in quoted)),
        "outstanding_balance": outstanding,
        "payment_status": customer_payment_status(active, outstanding),
        "last_order_at": max((order["created_at"] for order in orders), default=None),
        "last_order_date": iso(local_date(max((order["created_at"] for order in orders), default=None))),
    }


def _customer_view(params: Mapping[str, Any]) -> str:
    view = param_text(params, "view", 20) or "all"
    if view not in CUSTOMER_VIEWS:
        raise bad_request("Select a valid customer view (all, paid, or unpaid).")
    return view


def customer_rows(params: Mapping[str, Any], *, with_orders: bool = False) -> list[dict[str, Any]]:
    """Customer rows after the search, filters, and view. ``with_orders`` keeps each customer's order records
    (under ``_orders``) for reports that list the order and payment history."""
    customers = customer_repository.list_customers()
    orders = order_repository.list_orders()
    grouped = _orders_by_customer(orders, customers)
    view_status = CUSTOMER_VIEWS[_customer_view(params)]
    search = param_text(params, "search")
    activity = param_text(params, "activity", 40)
    if activity and activity not in CUSTOMER_ACTIVITY:
        raise bad_request("Select a valid order activity filter.")
    pay = param_text(params, "payment_status", 40)
    if pay and pay not in CUSTOMER_PAYMENT_STATUSES:
        raise bad_request("Select a valid customer payment status.")
    reg_start, reg_end = date_range(params, "registered_from", "registered_to", "Registration date")
    ord_start, ord_end = date_range(params, "ordered_from", "ordered_to", "Order date")
    rows = []
    for customer in customers:
        own = grouped.get(customer["id"], [])
        row = customer_stats(customer, own)
        if not matches(search, row["name"], row["email"], row["phone"], row["display_id"]):
            continue
        if not in_range(local_date(row["created_at"]), reg_start, reg_end):
            continue
        if (ord_start or ord_end) and not any(in_range(local_date(order["created_at"]), ord_start, ord_end) for order in own):
            continue
        if activity == "with_orders" and not row["total_orders"]:
            continue
        if activity == "no_orders" and row["total_orders"]:
            continue
        if activity == "with_pending" and not row["pending_orders"]:
            continue
        if activity == "with_completed" and not row["completed_orders"]:
            continue
        if activity == "repeat" and row["total_orders"] < 2:
            continue
        if activity == "with_spend" and row["total_spent"] <= 0:
            continue
        if activity == "no_spend" and row["total_spent"] > 0:
            continue
        if pay and row["payment_status"] != pay:
            continue
        if view_status and row["payment_status"] != view_status:
            continue
        if with_orders:
            row["_orders"] = own
        rows.append(row)
    return sort_rows(rows, params, CUSTOMER_SORTS, "registered")


def customer_list(params: Mapping[str, Any]) -> dict[str, Any]:
    rows = customer_rows(params)
    result = paginate(rows, params)
    # How many customers each view holds with the same search and filters (for the view tabs).
    result["view"] = _customer_view(params)
    result["view_counts"] = {
        view: len(rows) if view == result["view"] else len(customer_rows({**params, "view": view}))
        for view in CUSTOMER_VIEWS
    }
    result["summary"] = {
        "customers": len(rows),
        "total_spent": money(sum(row["total_spent"] for row in rows)),
        "total_transaction_amount": money(sum(row["total_transaction_amount"] for row in rows)),
        "total_paid": money(sum(row["total_paid"] for row in rows)),
        "outstanding_balance": money(sum(row["outstanding_balance"] for row in rows)),
        "with_orders": sum(1 for row in rows if row["total_orders"]),
    }
    result["payment_statuses"] = list(CUSTOMER_PAYMENT_STATUSES)
    return result


def customer_detail(customer_id: str) -> dict[str, Any]:
    customer = customer_repository.get_by_id(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    customer.pop("password_hash", None)
    orders, services = _load()
    own = _orders_by_customer(orders, [customer]).get(customer["id"], [])
    rows = [order_row(order, services) for order in own]
    payments = []
    for order in own:
        for index, payment in enumerate(order["payments"], start=1):
            verification = order_services.payment_verification(payment)
            payments.append({
                "id": f"{order['reference']}-P{index}",
                "reference": order["reference"],
                "payment_number": index,
                "product": (order.get("product") or {}).get("name", ""),
                "amount": money(payment.get("amount")),
                "method": payment.get("method", ""),
                "payment_reference": payment.get("reference", ""),
                "reference_type_label": verification["reference_type_label"],
                "verification_status": verification["status"],
                "received_at": payment.get("received_at"),
                "received_date": iso(local_date(payment.get("received_at"))),
            })
    payments.sort(key=lambda item: item["received_at"] or "", reverse=True)
    history: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = history.setdefault(row["service_id"], {"service_id": row["service_id"], "service": row["service"], "orders": 0, "completed": 0, "spent": 0.0, "last_order_date": None})
        entry["orders"] += 1
        entry["completed"] += row["status"] == "Completed"
        entry["spent"] = money(entry["spent"] + row["confirmed_revenue"])
        if entry["last_order_date"] is None or (row["request_date"] or "") > entry["last_order_date"]:
            entry["last_order_date"] = row["request_date"]
    return {
        "customer": customer_stats(customer, own),
        "orders": sorted(rows, key=lambda row: row["created_at"], reverse=True),
        "payments": payments,
        "service_history": sorted(history.values(), key=lambda item: (-item["orders"], item["service"])),
    }


def customer_print_report(params: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the printable customer report shows, for the customers matching the screen's view,
    search, and filters (or one customer with ``customer_id``): each customer's totals, and optionally
    the order history and the payment history with references and verification status."""
    include_history = str(params.get("include_history") or "").lower() in {"1", "true", "yes"}
    customer_id = param_text(params, "customer_id", 80)
    services = ServiceIndex()
    if customer_id:
        customer = customer_repository.get_by_id(customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        own = _orders_by_customer(order_repository.list_orders(), [customer]).get(customer["id"], [])
        row = customer_stats(customer, own)
        row["_orders"] = own
        rows = [row]
        include_history = True
    else:
        rows = customer_rows(params, with_orders=True)
    customers = []
    for row in rows:
        own = row.pop("_orders", [])
        entry = dict(row)
        if include_history:
            ordered = sorted(own, key=lambda order: order["created_at"], reverse=True)
            entry["orders"] = [
                {key: value for key, value in order_row(order, services).items() if key in {
                    "reference", "service", "product", "description", "request_date", "status", "total_amount",
                    "amount_paid", "balance", "payment_status", "completion_date",
                }}
                for order in ordered
            ]
            entry["payments"] = [
                {key: value for key, value in payment.items() if key in {
                    "reference", "product", "amount", "method", "payment_reference", "reference_type_label",
                    "verification_status", "received_at", "received_date", "entry_type",
                }}
                for order in ordered
                for payment in _transaction_entries(order, order_row(order, services))
            ]
        customers.append(entry)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "view": "single" if customer_id else _customer_view(params),
        "include_history": include_history,
        "customers": customers,
        "summary": {
            "customers": len(customers),
            "total_orders": sum(row["total_orders"] for row in customers),
            "total_transaction_amount": money(sum(row["total_transaction_amount"] for row in customers)),
            "total_paid": money(sum(row["total_paid"] for row in customers)),
            "outstanding_balance": money(sum(row["outstanding_balance"] for row in customers)),
            "total_spent": money(sum(row["total_spent"] for row in customers)),
        },
    }


# ================================================================== completed transactions


COMPLETED_SORTS = {
    "reference": "reference",
    "customer": "customer_name",
    "service": "service",
    "description": "description",
    "total": "total_amount",
    "paid": "amount_paid",
    "request_date": "created_at",
    "completion_date": "completed_at",
    "days": "completion_days",
    "payment_status": "payment_status",
}


def completed_rows(params: Mapping[str, Any], orders: list[OrderRecord] | None = None, services: ServiceIndex | None = None) -> tuple[list[dict[str, Any]], ServiceIndex]:
    if orders is None or services is None:
        orders, services = _load()
    service = param_text(params, "service", 80)
    customer = param_text(params, "customer")
    pay = param_text(params, "payment_status", 40)
    if pay and pay not in PAYMENT_STATUSES:
        raise bad_request("Select a valid payment status.")
    req_start, req_end = date_range(params, "request_from", "request_to", "Request date")
    done_start, done_end = date_range(params, "completed_from", "completed_to", "Completion date")
    min_days = param_int(params, "min_days", "Minimum duration", 0, 3650)
    max_days = param_int(params, "max_days", "Maximum duration", 0, 3650)
    if min_days is not None and max_days is not None and min_days > max_days:
        raise bad_request("The minimum duration must not be more than the maximum duration.")
    rows = []
    for order in orders:
        if not order_services.counts_as_completed(order):
            continue
        row = order_row(order, services)
        if service and row["service_id"] != service:
            continue
        if not matches(customer, row["customer_name"], row["customer_email"]):
            continue
        if pay and row["payment_status"] != pay:
            continue
        if not in_range(local_date(row["created_at"]), req_start, req_end):
            continue
        if not in_range(completion_date(order), done_start, done_end):
            continue
        days = row["completion_days"]
        if min_days is not None and (days is None or days < min_days):
            continue
        if max_days is not None and (days is None or days > max_days):
            continue
        rows.append(row)
    return sort_rows(rows, params, COMPLETED_SORTS, "completion_date"), services


def completed_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [row["completion_days"] for row in rows if row["completion_days"] is not None]
    return {
        "completed_orders": len(rows),
        "fully_paid_orders": sum(1 for row in rows if row["payment_status"] == "Fully Paid"),
        "total_order_amount": money(sum(row["total_amount"] or 0 for row in rows)),
        "total_amount_paid": money(sum(row["amount_paid"] for row in rows)),
        "confirmed_revenue": money(sum(row["confirmed_revenue"] for row in rows)),
        "average_completion_days": round(sum(durations) / len(durations), 1) if durations else None,
        "fastest_completion_days": min(durations) if durations else None,
        "slowest_completion_days": max(durations) if durations else None,
    }


def completed_list(params: Mapping[str, Any]) -> dict[str, Any]:
    orders, services = _load()
    rows, services = completed_rows(params, orders, services)
    result = paginate(rows, params)
    result["summary"] = completed_summary(rows)
    result["services"] = service_options(services, (order_row(order, services) for order in orders))
    return result


# ================================================================== service reports


def service_report(params: Mapping[str, Any]) -> dict[str, Any]:
    """Per-service summaries; with ``service`` set, also that service's completed transactions."""
    orders, services = _load()
    selected = param_text(params, "service", 80)
    base_params = {key: value for key, value in params.items() if key != "service"}
    all_completed, services = completed_rows({**base_params, "page_size": "all"}, orders, services)
    all_rows = [order_row(order, services) for order in orders]
    options = service_options(services, all_rows)
    summaries = []
    for service in options:
        completed = [row for row in all_completed if row["service_id"] == service["id"]]
        summary = completed_summary(completed)
        summary.update({
            "service_id": service["id"],
            "service": service["label"],
            "total_orders": sum(1 for row in all_rows if row["service_id"] == service["id"]),
        })
        summaries.append(summary)
    result: dict[str, Any] = {"services": options, "summaries": summaries}
    if selected:
        service = next((item for item in options if item["id"] == selected), None)
        if service is None:
            raise bad_request("Select a valid service.")
        rows = [row for row in all_completed if row["service_id"] == selected]
        page = paginate(rows, params)
        result.update({
            "selected": service,
            "summary": next(item for item in summaries if item["service_id"] == selected),
            **page,
        })
    return result


# ================================================================== business summary & revenue


PRESETS = {"all", "today", "week", "month", "year", "custom"}
GROUPINGS = {"day", "week", "month", "year"}
MAX_BUCKETS = 400


def resolve_period(params: Mapping[str, Any]) -> dict[str, Any]:
    preset = param_text(params, "preset", 20) or "all"
    if preset not in PRESETS:
        raise bad_request("Select a valid report period.")
    today = today_local()
    start: date | None
    end: date | None
    if preset == "today":
        start, end = today, today
    elif preset == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "year":
        start, end = today.replace(month=1, day=1), today
    elif preset == "custom":
        start, end = date_range(params, "date_from", "date_to", "Report period")
        if start is None or end is None:
            raise bad_request("Choose both a start and an end date for a custom period.")
    else:
        start, end = None, None
    group = param_text(params, "group_by", 10)
    if group and group not in GROUPINGS:
        raise bad_request("Select daily, weekly, monthly, or yearly grouping.")
    if not group:
        if preset in {"today", "week", "month"}:
            group = "day"
        elif preset == "custom" and start and end and (end - start).days <= 62:
            group = "day"
        else:
            group = "month"
    labels = {
        "all": "All time",
        "today": f"Today ({today:%b %d, %Y})",
        "week": "This week",
        "month": f"{today:%B %Y}",
        "year": f"{today:%Y}",
        "custom": "Custom range",
    }
    label = labels[preset]
    if start and end and preset != "today":
        label = f"{label}: {start:%b %d, %Y} – {end:%b %d, %Y}" if preset != "custom" else f"{start:%b %d, %Y} – {end:%b %d, %Y}"
    return {"preset": preset, "start": start, "end": end, "group_by": group, "label": label}


def _bucket_start(value: date, group: str) -> date:
    if group == "day":
        return value
    if group == "week":
        return value - timedelta(days=value.weekday())
    if group == "month":
        return value.replace(day=1)
    return value.replace(month=1, day=1)


def _next_bucket(value: date, group: str) -> date:
    if group == "day":
        return value + timedelta(days=1)
    if group == "week":
        return value + timedelta(days=7)
    if group == "month":
        return date(value.year + (value.month == 12), value.month % 12 + 1, 1)
    return date(value.year + 1, 1, 1)


def _bucket_label(value: date, group: str) -> str:
    if group == "day":
        return f"{value:%b %d, %Y}"
    if group == "week":
        return f"Week of {value:%b %d, %Y}"
    if group == "month":
        return f"{value:%b %Y}"
    return f"{value:%Y}"


def revenue_trend(completed: list[dict[str, Any]], start: date | None, end: date | None, group: str) -> list[dict[str, Any]]:
    dates = [date.fromisoformat(row["completion_date"]) for row in completed if row["completion_date"]]
    if start is None:
        start = min(dates, default=today_local())
    if end is None:
        end = max(dates + [today_local()])
    first = _bucket_start(start, group)
    buckets: list[dict[str, Any]] = []
    cursor = first
    while cursor <= end:
        if len(buckets) >= MAX_BUCKETS:
            raise bad_request("This period has too many points for that grouping. Choose a larger grouping (for example monthly).")
        following = _next_bucket(cursor, group)
        buckets.append({
            "key": cursor.isoformat(),
            "label": _bucket_label(cursor, group),
            "start": max(cursor, start).isoformat(),
            "end": min(following - timedelta(days=1), end).isoformat(),
            "revenue": 0.0,
            "completed_orders": 0,
            "fully_paid_completed": 0,
        })
        cursor = following
    index = {bucket["key"]: bucket for bucket in buckets}
    for row in completed:
        if not row["completion_date"]:
            continue
        bucket = index.get(_bucket_start(date.fromisoformat(row["completion_date"]), group).isoformat())
        if bucket is None:
            continue
        bucket["completed_orders"] += 1
        if row["confirmed_revenue"] > 0:
            bucket["fully_paid_completed"] += 1
            bucket["revenue"] = money(bucket["revenue"] + row["confirmed_revenue"])
    return buckets


def payment_summary(orders: list[OrderRecord], requested: list[dict[str, Any]], start: date | None, end: date | None) -> dict[str, Any]:
    """Money received in the period (by payment date) and how open orders are being paid.

    Payments on a 50% down payment order count as the down payment until half of the confirmed total
    is covered; the rest is the balance. Payments on Cash on Delivery orders are COD collections.
    Payments are cash flow, not revenue: revenue is still recognized only when an order is completed
    and fully paid.
    """
    received = down = balance = cod = delivery = 0.0
    count = 0
    methods: dict[str, dict[str, Any]] = {}
    refunds_completed = refunds_open = 0.0
    refund_count = 0
    for order in orders:
        for refund in order.get("refunds") or []:
            if refund.get("status") == "refunded" and in_range(local_date(refund.get("refunded_at")), start, end):
                refunds_completed += float(refund.get("amount") or 0)
                refund_count += 1
            elif refund.get("status") in {"approved", "processing", "failed"}:
                refunds_open += float(refund.get("amount") or 0)
        option = order_services.payment_option(order)
        total = order["total_amount"]
        threshold = float(total) * order_services.DOWN_PAYMENT_RATE if total is not None else 0.0
        # The delivery fee's share of each payment (payments cover the whole total, fee included, pro rata).
        fee_share = order_services.delivery_fee(order) / float(total) if total else 0.0
        cumulative = 0.0
        for payment in sorted(order["payments"], key=lambda item: item.get("received_at") or ""):
            amount = money(payment.get("amount"))
            if option == "cod":
                parts = (0.0, 0.0, amount)
            else:
                down_part = max(0.0, min(amount, threshold - cumulative))
                parts = (down_part, amount - down_part, 0.0)
            cumulative += amount
            if not in_range(local_date(payment.get("received_at")), start, end):
                continue
            count += 1
            received += amount
            down += parts[0]
            balance += parts[1]
            cod += parts[2]
            delivery += amount * fee_share
            method = payment.get("method") or "Other"
            entry = methods.setdefault(method, {"method": method, "count": 0, "amount": 0.0})
            entry["count"] += 1
            entry["amount"] = money(entry["amount"] + amount)
    active = [row for row in requested if row["status"] not in CANCELLED_STATUSES]
    return {
        "received_total": money(received),
        # Verified collections minus completed refunds (pending, rejected, and failed refunds are not deducted).
        "verified_collections": money(received),
        "refunds_completed": money(refunds_completed),
        "refund_count": refund_count,
        "refunds_in_progress": money(refunds_open),
        "net_collections": money(received - refunds_completed),
        "delivery_fee_collections": money(delivery),
        "payment_count": count,
        "down_payments": money(down),
        "balance_payments": money(balance),
        "cod_collections": money(cod),
        "by_method": sorted(methods.values(), key=lambda item: (-item["amount"], item["method"])),
        "orders_down_payment": sum(1 for row in active if row["payment_option"] == "down_payment"),
        "orders_cod": sum(1 for row in active if row["payment_option"] == "cod"),
        "outstanding_balance": money(sum(row["balance"] or 0 for row in active if row["price_agreed"] and row["status"] != "Completed")),
        "awaiting_down_payment": sum(1 for row in active if row["status"] == "Awaiting Down Payment"),
    }


def business_summary(params: Mapping[str, Any]) -> dict[str, Any]:
    """Overall performance for a period.

    Order counts use the request date. Completed orders and confirmed revenue use the completion
    date, so revenue is recognized once, when the order is finished and fully paid.
    """
    period = resolve_period(params)
    start, end = period["start"], period["end"]
    orders, services = _load()
    selected = param_text(params, "service", 80)
    rows = [order_row(order, services) for order in orders]
    options = service_options(services, rows)
    if selected and selected not in {item["id"] for item in options}:
        raise bad_request("Select a valid service.")
    if selected:
        rows = [row for row in rows if row["service_id"] == selected]
        orders = [order for order in orders if services.resolve(order)["id"] == selected]
    # Optional filters: fulfillment method, order status (or status group), payment method, customer.
    method = param_text(params, "fulfillment", 20)
    if method:
        if method not in FULFILLMENT_METHODS:
            raise bad_request("Select Store Pickup or Delivery.")
        rows = [row for row in rows if row["fulfillment_method"] == method]
        orders = [order for order in orders if order_services.fulfillment_method(order) == method]
    status_filter = param_text(params, "status", 60)
    if status_filter:
        group = STATUS_GROUPS.get(status_filter, {status_filter})
        rows = [row for row in rows if row["status"] in group]
        orders = [order for order in orders if order["status"] in group]
    customer_filter = param_text(params, "customer", 64)
    if customer_filter:
        rows = [row for row in rows if row["customer_id"] == customer_filter]
        orders = [order for order in orders if order.get("customer_id") == customer_filter]
    payment_method = param_text(params, "payment_method", 60)
    if payment_method:
        orders = [order for order in orders if any(item.get("method") == payment_method for item in order["payments"])]
        kept = {order["reference"] for order in orders}
        rows = [row for row in rows if row["reference"] in kept]
    requested = [row for row in rows if in_range(local_date(row["created_at"]), start, end)]
    completed = [
        row for row in rows
        if row["reported_completed"] and in_range(date.fromisoformat(row["completion_date"]) if row["completion_date"] else None, start, end)
    ]
    customers = customer_repository.list_customers()
    durations = [row["completion_days"] for row in completed if row["completion_days"] is not None]
    totals = {
        "total_customers": len(customers),
        "new_customers": sum(1 for customer in customers if in_range(local_date(customer["created_at"]), start, end)),
        "total_orders": len(requested),
        "pending_orders": sum(1 for row in requested if row["status"] in PENDING_STATUSES),
        "processing_orders": sum(1 for row in requested if row["status"] in PROCESSING_STATUSES),
        "cancelled_orders": sum(1 for row in requested if row["status"] in CANCELLED_STATUSES),
        "fully_paid_orders": sum(1 for row in requested if row["payment_status"] == "Fully Paid" and row["status"] not in CANCELLED_STATUSES),
        "completed_orders": len(completed),
        "confirmed_revenue": money(sum(row["confirmed_revenue"] for row in completed)),
        "average_completion_days": round(sum(durations) / len(durations), 1) if durations else None,
    }
    by_service = []
    for service in options:
        if selected and service["id"] != selected:
            continue
        service_requested = [row for row in requested if row["service_id"] == service["id"]]
        service_completed = [row for row in completed if row["service_id"] == service["id"]]
        service_durations = [row["completion_days"] for row in service_completed if row["completion_days"] is not None]
        revenue = money(sum(row["confirmed_revenue"] for row in service_completed))
        by_service.append({
            "service_id": service["id"],
            "service": service["label"],
            "total_orders": len(service_requested),
            "completed_orders": len(service_completed),
            "fully_paid_completed": sum(1 for row in service_completed if row["confirmed_revenue"] > 0),
            "revenue": revenue,
            "revenue_share": round(revenue / totals["confirmed_revenue"] * 100, 1) if totals["confirmed_revenue"] else 0.0,
            "average_completion_days": round(sum(service_durations) / len(service_durations), 1) if service_durations else None,
        })
    return {
        "period": {**period, "start": iso(start), "end": iso(end)},
        "service": next((item for item in options if item["id"] == selected), None),
        "services": options,
        "totals": totals,
        "by_service": by_service,
        "trend": revenue_trend(completed, start, end, period["group_by"]),
        "revenue_transactions": sorted(
            (row for row in completed if row["confirmed_revenue"] > 0),
            key=lambda row: row["completed_at"] or "",
            reverse=True,
        ),
        "payments": payment_summary(orders, requested, start, end),
    }


# ================================================================== notification badges


def action_counts() -> dict[str, Any]:
    """How many records wait for the administrator, for the sidebar and dashboard badges."""
    orders = order_repository.list_orders()
    customers = customer_repository.list_customers()
    counts = {status: 0 for status in STATUS_ORDER}
    for order in orders:
        counts[order["status"]] = counts.get(order["status"], 0) + 1
    week_ago = today_local() - timedelta(days=6)
    collect_before_release = sum(
        1 for order in orders
        if order["status"] in order_services.RELEASE_STATUSES and not is_fully_paid(order)
    )
    payments_to_verify = sum(
        1 for order in orders
        if order["status"] not in CANCELLED_STATUSES and order_services.pending_submissions(order)
    )
    new_requests = counts["Pending Confirmation"]
    # Requests the administrator must act on: review/quote, re-quote after a decline, confirm a COD arrangement.
    to_review = (
        counts["Under Review"] + counts["Awaiting Quotation"]
        + counts["Quotation Declined"] + counts["Awaiting COD Confirmation"]
    )
    awaiting_down_payment = counts["Awaiting Down Payment"]
    confirmed = counts["Confirmed"]
    new_customers = sum(1 for customer in customers if (local_date(customer["created_at"]) or date.min) >= week_ago)
    pending_cancellations = sum(1 for order in orders if order_services.pending_cancellation(order))
    pending_refunds = sum(
        1 for order in orders for refund in order.get("refunds") or [] if refund.get("status") in {"pending_review", "approved", "processing", "failed"}
    )
    open_issues = sum(1 for order in orders for issue in order_services.fulfillment(order).get("issues", []) if issue.get("status") == "open")
    return {
        "quality_checking": counts["Quality Checking"],
        "packaging": counts["Packaging"],
        "awaiting_full_payment": counts["Awaiting Full Payment"],
        "ready_for_dispatch": counts["Ready for Dispatch"],
        "delivery_failed": counts["Delivery Failed"],
        "delivered": counts["Delivered"],
        "order_claimed": counts["Order Claimed"],
        "pending_cancellations": pending_cancellations,
        "pending_refunds": pending_refunds,
        "open_delivery_issues": open_issues,
        "production": counts["Approved for Printing"] + counts["In Production"] + counts["Quality Checking"] + counts["Packaging"],
        "fulfillment": counts["Ready for Pickup"] + counts["Ready for Dispatch"] + counts["Delivery Failed"] + open_issues,
        "refunds": pending_cancellations + pending_refunds,
        "new_requests": new_requests,
        "to_review": to_review,
        "awaiting_down_payment": awaiting_down_payment,
        "confirmed": confirmed,
        "draft_preparation": counts["Draft Preparation"],
        "awaiting_customer_approval": counts["Awaiting Customer Approval"],
        "in_production": counts["Approved for Printing"] + counts["In Production"],
        "ready_for_pickup": counts["Ready for Pickup"],
        "out_for_delivery": counts["Out for Delivery"],
        "quotation_sent": counts["Quotation Sent"],
        "quotation_declined": counts["Quotation Declined"],
        "awaiting_cod_confirmation": counts["Awaiting COD Confirmation"],
        "payments_to_verify": payments_to_verify,
        "collect_before_release": collect_before_release,
        "new_customers": new_customers,
        "unread_notifications": unread_notification_count(orders, customers),
        # Sidebar badges.
        "orders": new_requests + to_review,
        "payments": payments_to_verify + collect_before_release,
        "customers": new_customers,
        # Messages customers sent to the administrator from the chat window, not resolved yet.
        "chatbot_inquiries": chatbot_repository.count_open_inquiries(),
        # Changes whenever any order or registration changes, so pages know to refresh.
        "signature": f"{len(orders)}:{max((order['updated_at'] for order in orders), default='')}:{len(customers)}",
        "status_counts": counts,
    }


# ================================================================== dashboard


# Dashboard workflow stages: every order that is not cancelled or rejected is in exactly one of them.
DASHBOARD_STAGES = {
    "new": ("Pending Confirmation",),
    "to_review": ("Under Review", "Awaiting Quotation", "Quotation Declined", "Awaiting COD Confirmation"),
    "awaiting_customer": ("Quotation Sent", "Awaiting Down Payment"),
    "production": (
        "Confirmed", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production",
        "Quality Checking", "Packaging", "Awaiting Full Payment",
    ),
    "release": ("Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed"),
    "completed": ("Completed",),
}
STATUS_GROUPS.update({key: set(value) for key, value in DASHBOARD_STAGES.items() if key != "completed"})


def dashboard() -> dict[str, Any]:
    """The administrator's essentials: where every active order is, payments to verify, and revenue.

    Every figure is counted from the orders table on each request.
    """
    orders, services = _load()
    rows = [order_row(order, services) for order in orders]
    stages = {key: sum(1 for row in rows if row["status"] in statuses) for key, statuses in DASHBOARD_STAGES.items()}
    completed = [row for row in rows if row["reported_completed"]]
    active = [order for order in orders if order["status"] not in CANCELLED_STATUSES]
    today = today_local()
    month_start = today.replace(day=1)
    first_month = date(today.year - (today.month <= 11), (today.month - 12) % 12 + 1, 1)  # 12 months incl. current
    received_this_month = [
        payment for order in orders for payment in order["payments"]
        if in_range(local_date(payment.get("received_at")), month_start, today)
    ]
    status_counts = {status: sum(1 for order in orders if order["status"] == status) for status in STATUS_ORDER}
    refunds_completed = money(sum(order_services.refunded_amount(order) for order in orders))
    received_total = money(sum(float(payment.get("amount") or 0) for order in orders for payment in order["payments"]))
    return {
        "stages": stages,
        "status_counts": status_counts,
        "pending_cancellations": sum(1 for order in orders if order_services.pending_cancellation(order)),
        "pending_refunds": sum(
            1 for order in orders for refund in order.get("refunds") or [] if refund.get("status") in {"pending_review", "approved", "processing", "failed"}
        ),
        "pending_orders": sum(1 for row in rows if row["status"] in PENDING_STATUSES),
        "collections": {"verified": received_total, "refunds_completed": refunds_completed, "net": money(received_total - refunds_completed)},
        "payments_to_verify": sum(1 for order in active if order_services.pending_submissions(order)),
        "payments_to_verify_amount": money(sum(order_services.pending_submission_amount(order) for order in active)),
        "payments_missing_reference": sum(
            1 for order in orders for payment in order["payments"] if not str(payment.get("reference") or "").strip()
        ),
        "cancelled_orders": sum(1 for row in rows if row["status"] in CANCELLED_STATUSES),
        "total_orders": len(rows),
        "revenue": {
            # Confirmed revenue only: completed and fully paid orders, each counted once, on the completion date.
            "total": money(sum(row["confirmed_revenue"] for row in rows)),
            "this_month": money(sum(
                row["confirmed_revenue"] for row in completed
                if in_range(local_date(row["completed_at"]), month_start, today)
            )),
            "received_this_month": money(sum(float(payment.get("amount") or 0) for payment in received_this_month)),
            "payments_this_month": len(received_this_month),
            "outstanding_balance": money(sum(
                max(0.0, float(order["total_amount"]) - paid_amount(order))
                for order in active if order_services.price_agreed(order)
            )),
            "month_label": month_start.strftime("%B %Y"),
        },
        "revenue_trend": revenue_trend(completed, first_month, today, "month"),
        "recent_orders": sorted(rows, key=lambda row: row["created_at"], reverse=True)[:8],
    }


# ================================================================== notifications / activity


def _classify(text: str) -> tuple[str, str]:
    rules: list[tuple[Callable[[str], bool], str, str]] = [
        (lambda value: value.startswith("Customer submitted a payment"), "Payment submitted for verification", "customer"),
        (lambda value: value.startswith("Customer submitted"), "New order request", "customer"),
        (lambda value: value.startswith("Customer approved"), "Draft approved by customer", "customer"),
        (lambda value: value.startswith("Customer requested draft changes"), "Customer requested draft changes", "customer"),
        (lambda value: value.startswith("Customer cancelled"), "Order cancelled by customer", "customer"),
        (lambda value: value.startswith("Customer accepted the quotation"), "Quotation accepted", "customer"),
        (lambda value: value.startswith("Customer declined the quotation"), "Quotation declined", "customer"),
        (lambda value: value.startswith("Customer requested cancellation"), "Cancellation requested", "customer"),
        (lambda value: value.startswith("Customer requested a refund"), "Refund requested", "customer"),
        (lambda value: value.startswith("Customer reported a delivery issue"), "Delivery issue reported", "customer"),
        (lambda value: "approved cancellation request" in value, "Cancellation approved", "admin"),
        (lambda value: "rejected cancellation request" in value, "Cancellation rejected", "admin"),
        (lambda value: "completed refund" in value, "Refund completed", "admin"),
        (lambda value: "refund" in value.lower() and value.startswith(("Administrator", "Refund")), "Refund updated", "admin"),
        (lambda value: "quality check" in value.lower(), "Quality check recorded", "admin"),
        (lambda value: "completed packaging" in value, "Packaging completed", "admin"),
        (lambda value: "dispatched the order" in value, "Order dispatched", "admin"),
        (lambda value: value.startswith("Delivery attempt") and "failed" in value, "Delivery failed", "admin"),
        (lambda value: "confirmed the delivery" in value, "Order delivered", "admin"),
        (lambda value: value.startswith("Order claimed"), "Order claimed", "admin"),
        (lambda value: "updated production" in value, "Production updated", "admin"),
        (lambda value: "the order is now Confirmed" in value, "Down payment verified · order confirmed", "admin"),
        (lambda value: "verified the customer's payment" in value, "Payment verified", "admin"),
        (lambda value: "could not verify the customer's payment" in value, "Payment submission rejected", "admin"),
        (lambda value: "confirmed the Cash on Delivery" in value, "COD arrangement confirmed", "admin"),
        (lambda value: "confirmed the package order" in value, "Package order confirmed", "admin"),
        (lambda value: "approved the package order" in value, "Package order approved · down payment requested", "admin"),
        (lambda value: "recorded PHP" in value, "Payment recorded", "admin"),
        (lambda value: "revised quotation of PHP" in value, "Quotation revised", "admin"),
        (lambda value: "quotation of PHP" in value, "Quotation sent", "admin"),
        (lambda value: "attached draft" in value, "Draft sent for approval", "admin"),
        (lambda value: "status to Completed" in value, "Order completed", "admin"),
        (lambda value: "status to" in value, "Order status updated", "admin"),
    ]
    for test, title, actor in rules:
        if test(text):
            return title, actor
    return ("Customer update", "customer") if text.startswith("Customer") else ("Order updated", "admin")


def _activity_events(orders: list[OrderRecord] | None = None, customers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every activity event (order history entries and registrations), newest first."""
    events = []
    for order in order_repository.list_orders() if orders is None else orders:
        for index, entry in enumerate(order["history"]):
            title, who = _classify(str(entry.get("text", "")))
            events.append({
                "id": f"{order['reference']}:{index}",
                "at": entry.get("at"),
                "title": title,
                "body": entry.get("text", ""),
                "reference": order["reference"],
                "customer_name": order["customer"].get("name", ""),
                "actor": who,
                "kind": "order",
            })
    for customer in customer_repository.list_customers() if customers is None else customers:
        events.append({
            "id": f"customer:{customer['id']}",
            "at": customer["created_at"],
            "title": "New customer registration",
            "body": f"{customer['name']} created a customer account.",
            "reference": None,
            "customer_id": customer["id"],
            "customer_name": customer["name"],
            "actor": "customer",
            "kind": "customer",
        })
    events.sort(key=lambda event: event["at"] or "", reverse=True)
    return events


def activity_feed(params: Mapping[str, Any]) -> dict[str, Any]:
    actor = param_text(params, "actor", 20)
    limit = param_int(params, "limit", "Limit", 1, 500) or 100
    events = _activity_events()
    if actor:
        events = [event for event in events if event["actor"] == actor]
    return {"events": events[:limit], "total": len(events)}


# ================================================================== administrator notifications
#
# Notifications are the customer activity above (new orders, payments submitted, quotation responses,
# draft responses, cancellations, registrations). Administrator actions are listed as activity but are
# never unread. Read state is stored in the database (app_meta), so it is the same on every device:
# everything up to ``seen_at`` is read, plus individually opened notifications in ``read_ids``.

NOTIFICATION_SEEN_KEY = "admin.notifications_seen_at"
NOTIFICATION_READ_KEY = "admin.notifications_read_ids"
NOTIFICATION_CATEGORIES = {
    "orders": "Orders",
    "payments": "Payments",
    "quotations": "Quotations & drafts",
    "customers": "Customer registrations",
}
MAX_READ_IDS = 2000


def _notification_category(event: Mapping[str, Any]) -> str:
    title = str(event.get("title", "")).lower()
    if event.get("kind") == "customer":
        return "customers"
    if "payment" in title or "refund" in title:
        return "payments"
    if "quotation" in title or "draft" in title:
        return "quotations"
    return "orders"


def _read_state() -> tuple[str, set[str]]:
    seen = admin_repository.get_setting(NOTIFICATION_SEEN_KEY) or ""
    try:
        read_ids = set(json.loads(admin_repository.get_setting(NOTIFICATION_READ_KEY) or "[]"))
    except (TypeError, ValueError):
        read_ids = set()
    return seen, read_ids


def _with_read_flags(events: list[dict[str, Any]], seen: str, read_ids: set[str]) -> list[dict[str, Any]]:
    result = []
    for event in events:
        unread = event["actor"] == "customer" and (event["at"] or "") > seen and event["id"] not in read_ids
        result.append({**event, "read": not unread, "category": _notification_category(event)})
    return result


def unread_notification_count(orders: list[OrderRecord] | None = None, customers: list[dict[str, Any]] | None = None) -> int:
    seen, read_ids = _read_state()
    return sum(1 for event in _with_read_flags(_activity_events(orders, customers), seen, read_ids) if not event["read"])


def notification_list(params: Mapping[str, Any]) -> dict[str, Any]:
    """The Notifications page: search, filters (actor, read status, category, dates), and pages."""
    actor = param_text(params, "actor", 20)
    status = param_text(params, "status", 20)
    category = param_text(params, "category", 20)
    search = param_text(params, "search")
    if actor and actor not in {"customer", "admin"}:
        raise bad_request("Select a valid notification source.")
    if status and status not in {"unread", "read"}:
        raise bad_request("Select a valid read status.")
    if category and category not in NOTIFICATION_CATEGORIES:
        raise bad_request("Select a valid notification category.")
    start, end = date_range(params, "date_from", "date_to", "Notification date")
    seen, read_ids = _read_state()
    events = _with_read_flags(_activity_events(), seen, read_ids)
    unread_total = sum(1 for event in events if not event["read"])
    rows = []
    for event in events:
        if actor and event["actor"] != actor:
            continue
        if status == "unread" and event["read"]:
            continue
        if status == "read" and not event["read"]:
            continue
        if category and event["category"] != category:
            continue
        if not in_range(local_date(event["at"]), start, end):
            continue
        if not matches(search, event["title"], event["body"], event["reference"], event["customer_name"]):
            continue
        rows.append(event)
    result = paginate(rows, params)
    result["unread_count"] = unread_total
    result["categories"] = [{"id": key, "label": label} for key, label in NOTIFICATION_CATEGORIES.items()]
    return result


def recent_notifications(limit: int = 6) -> dict[str, Any]:
    """The header bell: the latest customer notifications and the unread count."""
    seen, read_ids = _read_state()
    events = [event for event in _with_read_flags(_activity_events(), seen, read_ids) if event["actor"] == "customer"]
    return {
        "items": events[:limit],
        "unread_count": sum(1 for event in events if not event["read"]),
        "latest_at": events[0]["at"] if events else None,
    }


def mark_notifications_read(ids: Any = None, mark_all: bool = False, before: Any = None) -> dict[str, Any]:
    """Mark notifications read: all of them, all up to ``before`` (an ISO time), or the given ids."""
    seen, read_ids = _read_state()
    if mark_all or before:
        cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if before and not mark_all:
            parsed = parse_timestamp(before)
            if parsed is None:
                raise bad_request("Invalid notification time.")
            cutoff = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        if cutoff > seen:
            seen = cutoff
            admin_repository.set_setting(NOTIFICATION_SEEN_KEY, seen)
    if ids is not None:
        if not isinstance(ids, list) or len(ids) > 500 or not all(isinstance(item, str) and len(item) <= 120 for item in ids):
            raise bad_request("Invalid notification list.")
        read_ids.update(ids)
    # Keep only ids that the seen time does not already cover (event ids hold no time, so keep the newest).
    known = {event["id"]: event["at"] or "" for event in _activity_events()}
    read_ids = {item for item in read_ids if item in known and known[item] > seen}
    if len(read_ids) > MAX_READ_IDS:
        read_ids = set(sorted(read_ids, key=lambda item: known[item], reverse=True)[:MAX_READ_IDS])
    admin_repository.set_setting(NOTIFICATION_READ_KEY, json.dumps(sorted(read_ids)))
    return {"unread_count": unread_notification_count()}
