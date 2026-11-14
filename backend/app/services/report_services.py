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

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from backend.app.models.order import FULFILLMENT_METHODS, PAYMENT_OPTIONS, OrderRecord
from backend.app.repositories import catalog_repository, customer_repository, order_repository
from backend.app.services import order_services
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
    return order["status"] == "Completed" and is_fully_paid(order)


def confirmed_revenue(order: Mapping[str, Any]) -> float:
    """The order's confirmed total, counted once, only for completed and fully paid orders."""
    return money(order["total_amount"]) if is_confirmed_revenue(order) else 0.0


def completion_date(order: Mapping[str, Any]) -> date | None:
    if order["status"] != "Completed":
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
        "product_image": (order.get("product") or {}).get("image", ""),
        "order_kind": customization.get("order_kind", ""),
        "package_name": package.get("name", ""),
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
        "price_agreed": order_services.price_agreed(order),
        "completed_at": order.get("completed_at") if order["status"] == "Completed" else None,
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
    chosen = bool(order_services.payment_option(order))
    payable = status in order_services.PAYABLE_STATUSES and total is not None and chosen and paid < total - 0.005
    view["actions"] = {
        "quote": order_services.quotable(order),
        "confirm_package": order_services.is_fixed_price(order) and status in {"Pending Confirmation", "Under Review"},
        "confirm_cod": status == "Awaiting COD Confirmation" and order_services.is_cod(order),
        "verify_payments": payable and bool(pending),
        "record_payment": payable and not pending,
        "send_draft": status in {"Draft Preparation", "Awaiting Customer Approval"} and order_services.down_payment_met(order),
        "start_production_blocked": status == "Approved for Printing" and not order_services.down_payment_met(order),
        "complete_requires_payment": status in order_services.RELEASE_STATUSES and not is_fully_paid(order),
        "cancel_blocked_by_payments": bool(order["payments"]) or bool(pending),
        "change_payment_option": order_services.payment_option_changeable(order) and status not in CANCELLED_STATUSES,
    }
    view["payment_methods"] = list(order_services.PAYMENT_METHODS)
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
    pay = param_text(params, "payment_status", 40)
    kind = param_text(params, "kind", 20)
    option = param_text(params, "payment_option", 20)
    if kind and kind not in {"package", "customize"}:
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
        if status:
            group = STATUS_GROUPS.get(status)
            if (group is not None and row["status"] not in group) or (group is None and row["status"] != status):
                continue
        if service and row["service_id"] != service:
            continue
        if pay and row["payment_status"] != pay:
            continue
        if not in_range(local_date(row["created_at"]), start, end):
            continue
        if not matches(search, row["reference"], row["customer_name"], row["customer_email"], row["product"], row["service"]):
            continue
        rows.append(row)
    return sort_rows(rows, params, ORDER_SORTS, "request_date"), services


def order_list(params: Mapping[str, Any]) -> dict[str, Any]:
    orders, services = _load()
    rows, services = order_rows(params, orders, services)
    result = paginate(rows, params)
    result["services"] = service_options(services, (order_row(order, services) for order in orders))
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
}


def payment_rows(params: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[OrderRecord], ServiceIndex]:
    orders, services = _load()
    search = param_text(params, "search")
    method = param_text(params, "method", 60)
    service = param_text(params, "service", 80)
    pay = param_text(params, "payment_status", 40)
    option = param_text(params, "payment_option", 20)
    if option and option not in PAYMENT_OPTIONS:
        raise bad_request("Select a valid payment option.")
    start, end = date_range(params, "date_from", "date_to", "Payment date")
    rows = []
    for order in orders:
        base = order_row(order, services)
        if option and base["payment_option"] != option:
            continue
        for index, payment in enumerate(order["payments"], start=1):
            row = {
                "id": f"{order['reference']}-P{index}",
                "reference": order["reference"],
                "customer_name": base["customer_name"],
                "customer_email": base["customer_email"],
                "service_id": base["service_id"],
                "service": base["service"],
                "product": base["product"],
                "amount": money(payment.get("amount")),
                "method": payment.get("method", ""),
                "payment_reference": payment.get("reference", ""),
                "received_at": payment.get("received_at"),
                "received_date": iso(local_date(payment.get("received_at"))),
                "order_status": base["status"],
                "order_total": base["total_amount"],
                "order_paid": base["amount_paid"],
                "order_balance": base["balance"],
                "order_payment_status": base["payment_status"],
                "payment_option": base["payment_option"],
                "payment_option_label": base["payment_option_label"],
                "payment_number": index,
            }
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
    result["summary"] = {
        "filtered_amount": money(sum(row["amount"] for row in rows)),
        "filtered_count": len(rows),
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


def customer_rows(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    customers = customer_repository.list_customers()
    orders = order_repository.list_orders()
    grouped = _orders_by_customer(orders, customers)
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
        rows.append(row)
    return sort_rows(rows, params, CUSTOMER_SORTS, "registered")


def customer_list(params: Mapping[str, Any]) -> dict[str, Any]:
    rows = customer_rows(params)
    result = paginate(rows, params)
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
            payments.append({
                "id": f"{order['reference']}-P{index}",
                "reference": order["reference"],
                "product": (order.get("product") or {}).get("name", ""),
                "amount": money(payment.get("amount")),
                "method": payment.get("method", ""),
                "payment_reference": payment.get("reference", ""),
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
        if order["status"] != "Completed":
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
    received = down = balance = cod = 0.0
    count = 0
    methods: dict[str, dict[str, Any]] = {}
    for order in orders:
        option = order_services.payment_option(order)
        total = order["total_amount"]
        threshold = float(total) * order_services.DOWN_PAYMENT_RATE if total is not None else 0.0
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
            method = payment.get("method") or "Other"
            entry = methods.setdefault(method, {"method": method, "count": 0, "amount": 0.0})
            entry["count"] += 1
            entry["amount"] = money(entry["amount"] + amount)
    active = [row for row in requested if row["status"] not in CANCELLED_STATUSES]
    return {
        "received_total": money(received),
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
    requested = [row for row in rows if in_range(local_date(row["created_at"]), start, end)]
    completed = [
        row for row in rows
        if row["status"] == "Completed" and in_range(date.fromisoformat(row["completion_date"]) if row["completion_date"] else None, start, end)
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
    new_customers = sum(1 for customer in customers if (local_date(customer["created_at"]) or date.min) >= week_ago)
    return {
        "new_requests": new_requests,
        "to_review": to_review,
        "awaiting_down_payment": awaiting_down_payment,
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
        # Sidebar badges.
        "orders": new_requests + to_review,
        "payments": payments_to_verify + collect_before_release,
        "customers": new_customers,
        # Changes whenever any order or registration changes, so pages know to refresh.
        "signature": f"{len(orders)}:{max((order['updated_at'] for order in orders), default='')}:{len(customers)}",
    }


# ================================================================== dashboard


def dashboard() -> dict[str, Any]:
    orders, services = _load()
    rows = [order_row(order, services) for order in orders]
    options = service_options(services, rows)
    status_counts = {status: 0 for status in STATUS_ORDER}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    completed = [row for row in rows if row["status"] == "Completed"]
    today = today_local()
    first_month = date(today.year - (today.month <= 11), (today.month - 12) % 12 + 1, 1)  # 12 months incl. current
    by_service = []
    for service in options:
        own = [row for row in rows if row["service_id"] == service["id"]]
        by_service.append({
            "service_id": service["id"],
            "service": service["label"],
            "orders": len(own),
            "completed_orders": sum(1 for row in own if row["status"] == "Completed"),
            "revenue": money(sum(row["confirmed_revenue"] for row in own)),
        })
    return {
        "total_customers": len(customer_repository.list_customers()),
        "total_orders": len(rows),
        "pending_orders": sum(1 for row in rows if row["status"] in PENDING_STATUSES),
        "processing_orders": sum(1 for row in rows if row["status"] in PROCESSING_STATUSES),
        "completed_orders": len(completed),
        "cancelled_orders": sum(1 for row in rows if row["status"] in CANCELLED_STATUSES),
        "fully_paid_orders": sum(1 for row in rows if row["payment_status"] == "Fully Paid" and row["status"] not in CANCELLED_STATUSES),
        # Confirmed revenue only: completed and fully paid orders, each counted once.
        "total_revenue": money(sum(row["confirmed_revenue"] for row in rows)),
        "status_counts": status_counts,
        "by_service": by_service,
        "revenue_trend": revenue_trend(completed, first_month, today, "month"),
        "recent_orders": sorted(rows, key=lambda row: row["created_at"], reverse=True)[:8],
        "recent_completed": sorted(completed, key=lambda row: row["completed_at"] or "", reverse=True)[:5],
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
        (lambda value: "verified the customer's payment" in value, "Payment verified", "admin"),
        (lambda value: "could not verify the customer's payment" in value, "Payment submission rejected", "admin"),
        (lambda value: "confirmed the Cash on Delivery" in value, "COD arrangement confirmed", "admin"),
        (lambda value: "confirmed the package order" in value, "Package order confirmed", "admin"),
        (lambda value: "recorded PHP" in value, "Payment recorded", "admin"),
        (lambda value: "quotation of PHP" in value, "Quotation sent", "admin"),
        (lambda value: "attached draft" in value, "Draft sent for approval", "admin"),
        (lambda value: "status to Completed" in value, "Order completed", "admin"),
        (lambda value: "status to" in value, "Order status updated", "admin"),
    ]
    for test, title, actor in rules:
        if test(text):
            return title, actor
    return ("Customer update", "customer") if text.startswith("Customer") else ("Order updated", "admin")


def activity_feed(params: Mapping[str, Any]) -> dict[str, Any]:
    actor = param_text(params, "actor", 20)
    limit = param_int(params, "limit", "Limit", 1, 500) or 100
    events = []
    for order in order_repository.list_orders():
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
    for customer in customer_repository.list_customers():
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
    if actor:
        events = [event for event in events if event["actor"] == actor]
    events.sort(key=lambda event: event["at"] or "", reverse=True)
    return {"events": events[:limit], "total": len(events)}
