"""PDF and Excel exports of the admin reports.

Each export runs the same query function as the on-screen report with the same filters, but with
every matching row (no pagination), so the file always matches what the administrator selected.
"""
from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response

from backend.app.services import report_services as reports
from backend.app.utils.order_number import SHOP_TIMEZONE


BUSINESS_NAME = "ImPRESSion PrintShop"
FORMATS = {"pdf", "xlsx"}


@dataclass
class Column:
    key: str
    label: str
    kind: str = "text"  # text | date | money | int | days | percent
    width: float = 1.0  # relative width in the PDF


@dataclass
class Section:
    title: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] | None = None
    sheet: str = ""
    note: str = ""


@dataclass
class Report:
    title: str
    slug: str
    period: str
    filters: list[tuple[str, str]]
    summary: list[tuple[str, Any, str]]  # label, value, kind
    sections: list[Section] = field(default_factory=list)


# ================================================================== shared column sets


def completed_columns() -> list[Column]:
    return [
        Column("reference", "Transaction ID", width=1.3),
        Column("customer_name", "Customer", width=1.3),
        Column("service", "Service", width=1.2),
        Column("description", "Product / order description", width=2.6),
        Column("total_amount", "Total amount", "money", 1.0),
        Column("amount_paid", "Amount paid", "money", 1.0),
        Column("request_date", "Request date", "date", 0.95),
        Column("completion_date", "Completion date", "date", 0.95),
        Column("completion_days", "Duration (days)", "days", 0.75),
        Column("payment_status", "Payment status", width=0.95),
    ]


def completed_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = reports.completed_summary(rows)
    return {
        "reference": f"Total ({len(rows)} transactions)",
        "total_amount": summary["total_order_amount"],
        "amount_paid": summary["total_amount_paid"],
        "completion_days": f"Avg {summary['average_completion_days']:g} days" if summary["average_completion_days"] is not None else "",
    }


def completed_summary_items(rows: list[dict[str, Any]]) -> list[tuple[str, Any, str]]:
    summary = reports.completed_summary(rows)
    return [
        ("Completed transactions", summary["completed_orders"], "int"),
        ("Fully paid", summary["fully_paid_orders"], "int"),
        ("Total order amount", summary["total_order_amount"], "money"),
        ("Total amount paid", summary["total_amount_paid"], "money"),
        ("Confirmed revenue (completed and fully paid)", summary["confirmed_revenue"], "money"),
        ("Average completion duration (calendar days)", summary["average_completion_days"], "days"),
    ]


def _date_text(value: date | None) -> str:
    return f"{value:%b %d, %Y}" if value else ""


def _range_text(params: Mapping[str, Any], start_key: str, end_key: str) -> str:
    start = reports.param_date(params, start_key, "Start date")
    end = reports.param_date(params, end_key, "End date")
    if start and end:
        return f"{_date_text(start)} – {_date_text(end)}"
    if start:
        return f"From {_date_text(start)}"
    if end:
        return f"Up to {_date_text(end)}"
    return ""


def _service_label(params: Mapping[str, Any]) -> str:
    service_id = reports.param_text(params, "service", 80)
    if not service_id:
        return ""
    return reports.service_label(service_id)


def _filters(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    shown = [(label, value) for label, value in pairs if value]
    return shown or [("Filters", "None (all records)")]


# ================================================================== report builders


def build_completed(params: Mapping[str, Any]) -> Report:
    rows, _ = reports.completed_rows(params)
    days = []
    if params.get("min_days"):
        days.append(f"at least {params['min_days']}")
    if params.get("max_days"):
        days.append(f"at most {params['max_days']}")
    filters = _filters([
        ("Service", _service_label(params)),
        ("Customer", reports.param_text(params, "customer")),
        ("Request date", _range_text(params, "request_from", "request_to")),
        ("Completion date", _range_text(params, "completed_from", "completed_to")),
        ("Payment status", reports.param_text(params, "payment_status", 40)),
        ("Completion duration (days)", " and ".join(days)),
    ])
    period = _range_text(params, "completed_from", "completed_to") or "All completion dates"
    return Report(
        "Completed Transactions Report",
        "completed-transactions",
        period,
        filters,
        completed_summary_items(rows),
        [Section("Completed transactions", completed_columns(), rows, completed_totals(rows), "Completed Transactions")],
    )


def build_service(params: Mapping[str, Any]) -> Report:
    service_id = reports.param_text(params, "service", 80)
    base = {key: value for key, value in params.items() if key != "service"}
    rows, services = reports.completed_rows(base)
    data = reports.service_report({**base, "page_size": "all"})
    filters = [
        ("Request date", _range_text(params, "request_from", "request_to")),
        ("Completion date", _range_text(params, "completed_from", "completed_to")),
        ("Customer", reports.param_text(params, "customer")),
        ("Payment status", reports.param_text(params, "payment_status", 40)),
    ]
    period = _range_text(params, "completed_from", "completed_to") or "All completion dates"
    if service_id and service_id != "all":
        summary = next((item for item in data["summaries"] if item["service_id"] == service_id), None)
        if summary is None:
            raise reports.bad_request("Select a valid service.")
        own = [row for row in rows if row["service_id"] == service_id]
        return Report(
            f"{summary['service']} Service Report",
            f"service-report-{service_id}",
            period,
            _filters([("Service", summary["service"]), *filters]),
            [
                ("Service", summary["service"], "text"),
                ("Total completed orders", summary["completed_orders"], "int"),
                ("Total fully paid orders", summary["fully_paid_orders"], "int"),
                ("Total revenue (completed and fully paid)", summary["confirmed_revenue"], "money"),
                ("Average completion duration (calendar days)", summary["average_completion_days"], "days"),
            ],
            [Section(f"{summary['service']} — completed transactions", completed_columns(), own, completed_totals(own), summary["service"])],
        )
    # Combined report: an overview table plus one worksheet / section per service.
    overview_columns = [
        Column("service", "Service", width=2),
        Column("completed_orders", "Completed orders", "int"),
        Column("fully_paid_orders", "Fully paid orders", "int"),
        Column("confirmed_revenue", "Total revenue", "money"),
        Column("average_completion_days", "Avg completion (days)", "days"),
    ]
    summaries = data["summaries"]
    sections = [Section(
        "Service overview",
        overview_columns,
        summaries,
        {
            "service": "All services",
            "completed_orders": sum(item["completed_orders"] for item in summaries),
            "fully_paid_orders": sum(item["fully_paid_orders"] for item in summaries),
            "confirmed_revenue": reports.money(sum(item["confirmed_revenue"] for item in summaries)),
        },
        "Overview",
    )]
    for item in summaries:
        own = [row for row in rows if row["service_id"] == item["service_id"]]
        sections.append(Section(
            f"{item['service']} — completed transactions",
            completed_columns(),
            own,
            completed_totals(own),
            item["service"],
            "" if own else "No completed transactions for this service in the selected filters.",
        ))
    del services
    return Report(
        "Service Reports — All Printing Services",
        "service-reports-all",
        period,
        _filters([("Service", "All services"), *filters]),
        completed_summary_items(rows),
        sections,
    )


def build_customers(params: Mapping[str, Any]) -> Report:
    rows = reports.customer_rows(params)
    activity_labels = {
        "with_orders": "Has orders",
        "no_orders": "No orders yet",
        "with_pending": "Has pending orders",
        "with_completed": "Has completed orders",
        "repeat": "Repeat customers (2+ orders)",
        "with_spend": "Has confirmed spending",
        "no_spend": "No confirmed spending",
    }
    columns = [
        Column("display_id", "Customer ID", width=1.1),
        Column("name", "Customer name", width=1.4),
        Column("email", "Email address", width=1.9),
        Column("phone", "Contact number", width=1.2),
        Column("registered_date", "Registered", "date", 1.0),
        Column("total_orders", "Total orders", "int", 0.7),
        Column("completed_orders", "Completed", "int", 0.75),
        Column("pending_orders", "Pending", "int", 0.7),
        Column("total_spent", "Amount spent", "money", 1.0),
    ]
    return Report(
        "Customer Transaction Report",
        "customer-transactions",
        _range_text(params, "ordered_from", "ordered_to") or "All dates",
        _filters([
            ("Search", reports.param_text(params, "search")),
            ("Registration date", _range_text(params, "registered_from", "registered_to")),
            ("Ordered between", _range_text(params, "ordered_from", "ordered_to")),
            ("Order activity", activity_labels.get(reports.param_text(params, "activity", 40), "")),
        ]),
        [
            ("Customers", len(rows), "int"),
            ("Total orders", sum(row["total_orders"] for row in rows), "int"),
            ("Completed orders", sum(row["completed_orders"] for row in rows), "int"),
            ("Total amount spent (completed and fully paid)", reports.money(sum(row["total_spent"] for row in rows)), "money"),
        ],
        [Section("Customers", columns, rows, {
            "display_id": f"Total ({len(rows)} customers)",
            "total_orders": sum(row["total_orders"] for row in rows),
            "completed_orders": sum(row["completed_orders"] for row in rows),
            "pending_orders": sum(row["pending_orders"] for row in rows),
            "total_spent": reports.money(sum(row["total_spent"] for row in rows)),
        }, "Customers")],
    )


def build_customer(params: Mapping[str, Any]) -> Report:
    customer_id = reports.param_text(params, "customer_id", 80)
    if not customer_id:
        raise reports.bad_request("Choose a customer to export.")
    detail = reports.customer_detail(customer_id)
    customer = detail["customer"]
    order_columns = [
        Column("reference", "Order ID", width=1.3),
        Column("service", "Service", width=1.2),
        Column("description", "Product / order description", width=2.6),
        Column("request_date", "Request date", "date"),
        Column("status", "Status", width=1.2),
        Column("total_amount", "Total amount", "money"),
        Column("amount_paid", "Amount paid", "money"),
        Column("payment_status", "Payment status"),
        Column("completion_date", "Completion date", "date"),
        Column("completion_days", "Duration (days)", "days", 0.7),
    ]
    payment_columns = [
        Column("received_date", "Date received", "date"),
        Column("reference", "Order ID", width=1.3),
        Column("product", "Product", width=1.8),
        Column("method", "Method"),
        Column("payment_reference", "Payment reference", width=1.4),
        Column("amount", "Amount", "money"),
    ]
    service_columns = [
        Column("service", "Service", width=2),
        Column("orders", "Orders", "int"),
        Column("completed", "Completed", "int"),
        Column("spent", "Amount spent", "money"),
        Column("last_order_date", "Last order", "date"),
    ]
    orders = detail["orders"]
    payments = detail["payments"]
    return Report(
        f"Customer Transaction Report — {customer['name']}",
        f"customer-{customer['display_id'].lower()}",
        "All dates",
        [("Customer ID", customer["display_id"]), ("Email", customer["email"]), ("Contact number", customer["phone"] or "—")],
        [
            ("Registered", reports.local_date(customer["created_at"]), "date"),
            ("Total orders", customer["total_orders"], "int"),
            ("Completed orders", customer["completed_orders"], "int"),
            ("Pending orders", customer["pending_orders"], "int"),
            ("Total amount spent (completed and fully paid)", customer["total_spent"], "money"),
        ],
        [
            Section("Order history", order_columns, orders, {
                "reference": f"Total ({len(orders)} orders)",
                "total_amount": reports.money(sum(row["total_amount"] or 0 for row in orders)),
                "amount_paid": reports.money(sum(row["amount_paid"] for row in orders)),
            }, "Orders"),
            Section("Payment history", payment_columns, payments, {
                "received_date": f"Total ({len(payments)} payments)",
                "amount": reports.money(sum(row["amount"] for row in payments)),
            }, "Payments"),
            Section("Service history", service_columns, detail["service_history"], None, "Services"),
        ],
    )


def _summary_sections(data: dict[str, Any]) -> list[Section]:
    service_columns = [
        Column("service", "Service", width=2),
        Column("total_orders", "Orders requested", "int"),
        Column("completed_orders", "Completed orders", "int"),
        Column("fully_paid_completed", "Completed & fully paid", "int"),
        Column("revenue", "Confirmed revenue", "money"),
        Column("revenue_share", "Share of revenue", "percent"),
        Column("average_completion_days", "Avg completion (days)", "days"),
    ]
    trend_columns = [
        Column("label", "Period", width=1.6),
        Column("start", "From", "date"),
        Column("end", "To", "date"),
        Column("completed_orders", "Completed orders", "int"),
        Column("fully_paid_completed", "Completed & fully paid", "int"),
        Column("revenue", "Confirmed revenue", "money"),
    ]
    by_service = data["by_service"]
    trend = data["trend"]
    return [
        Section("Revenue by service", service_columns, by_service, {
            "service": "All services",
            "total_orders": sum(item["total_orders"] for item in by_service),
            "completed_orders": sum(item["completed_orders"] for item in by_service),
            "fully_paid_completed": sum(item["fully_paid_completed"] for item in by_service),
            "revenue": reports.money(sum(item["revenue"] for item in by_service)),
        }, "Revenue by Service"),
        Section(f"Revenue trend ({data['period']['group_by']}ly)".replace("dayly", "daily"), trend_columns, trend, {
            "label": "Total",
            "completed_orders": sum(item["completed_orders"] for item in trend),
            "fully_paid_completed": sum(item["fully_paid_completed"] for item in trend),
            "revenue": reports.money(sum(item["revenue"] for item in trend)),
        }, "Revenue Trend"),
    ]


def _summary_filters(data: dict[str, Any]) -> list[tuple[str, str]]:
    grouping = {"day": "Daily", "week": "Weekly", "month": "Monthly", "year": "Yearly"}[data["period"]["group_by"]]
    return [
        ("Service", data["service"]["label"] if data["service"] else "All services"),
        ("Grouping", grouping),
        ("Counting rules", "Orders by request date; completed orders and revenue by completion date"),
    ]


def build_summary(params: Mapping[str, Any]) -> Report:
    data = reports.business_summary(params)
    totals = data["totals"]
    return Report(
        "Overall Business Summary",
        "business-summary",
        data["period"]["label"],
        _summary_filters(data),
        [
            ("Total customers (registered accounts)", totals["total_customers"], "int"),
            ("New customers in period", totals["new_customers"], "int"),
            ("Total orders", totals["total_orders"], "int"),
            ("Completed orders", totals["completed_orders"], "int"),
            ("Pending orders", totals["pending_orders"], "int"),
            ("In-progress orders", totals["processing_orders"], "int"),
            ("Cancelled / rejected orders", totals["cancelled_orders"], "int"),
            ("Fully paid orders", totals["fully_paid_orders"], "int"),
            ("Total confirmed revenue", totals["confirmed_revenue"], "money"),
            ("Average completion duration (calendar days)", totals["average_completion_days"], "days"),
        ],
        _summary_sections(data),
    )


def build_revenue(params: Mapping[str, Any]) -> Report:
    data = reports.business_summary(params)
    transactions = data["revenue_transactions"]
    columns = [
        Column("completion_date", "Completion date", "date"),
        Column("reference", "Transaction ID", width=1.3),
        Column("customer_name", "Customer", width=1.3),
        Column("service", "Service", width=1.2),
        Column("description", "Product / order description", width=2.4),
        Column("confirmed_revenue", "Revenue", "money"),
    ]
    sections = _summary_sections(data)
    sections.reverse()
    sections.append(Section("Revenue transactions (completed and fully paid)", columns, transactions, {
        "completion_date": f"Total ({len(transactions)} transactions)",
        "confirmed_revenue": reports.money(sum(row["confirmed_revenue"] for row in transactions)),
    }, "Revenue Transactions"))
    return Report(
        "Revenue Report",
        "revenue-report",
        data["period"]["label"],
        _summary_filters(data),
        [
            ("Total confirmed revenue", data["totals"]["confirmed_revenue"], "money"),
            ("Completed and fully paid transactions", len(transactions), "int"),
            ("Completed orders in period", data["totals"]["completed_orders"], "int"),
        ],
        sections,
    )


def build_orders(params: Mapping[str, Any]) -> Report:
    rows, _ = reports.order_rows(params)
    columns = [
        Column("reference", "Order ID", width=1.3),
        Column("customer_name", "Customer", width=1.3),
        Column("service", "Service", width=1.2),
        Column("description", "Product / order description", width=2.4),
        Column("request_date", "Request date", "date"),
        Column("status", "Status", width=1.2),
        Column("total_amount", "Confirmed total", "money"),
        Column("amount_paid", "Amount paid", "money"),
        Column("payment_status", "Payment status"),
    ]
    status = reports.param_text(params, "status", 60)
    return Report(
        "Order Management Report",
        "orders",
        _range_text(params, "date_from", "date_to") or "All request dates",
        _filters([
            ("Search", reports.param_text(params, "search")),
            ("Status", {"pending": "Pending", "processing": "In progress", "completed": "Completed", "cancelled": "Cancelled / rejected"}.get(status, status)),
            ("Service", _service_label(params)),
            ("Payment status", reports.param_text(params, "payment_status", 40)),
            ("Request date", _range_text(params, "date_from", "date_to")),
        ]),
        [("Orders", len(rows), "int"), ("Total amount paid", reports.money(sum(row["amount_paid"] for row in rows)), "money")],
        [Section("Orders", columns, rows, {
            "reference": f"Total ({len(rows)} orders)",
            "total_amount": reports.money(sum(row["total_amount"] or 0 for row in rows)),
            "amount_paid": reports.money(sum(row["amount_paid"] for row in rows)),
        }, "Orders")],
    )


def build_payments(params: Mapping[str, Any]) -> Report:
    rows, _, _ = reports.payment_rows(params)
    columns = [
        Column("received_date", "Date received", "date"),
        Column("reference", "Order ID", width=1.3),
        Column("customer_name", "Customer", width=1.3),
        Column("service", "Service", width=1.2),
        Column("method", "Method"),
        Column("payment_reference", "Payment reference", width=1.3),
        Column("amount", "Amount received", "money"),
        Column("order_payment_status", "Order payment status"),
    ]
    return Report(
        "Payment Transactions Report",
        "payments",
        _range_text(params, "date_from", "date_to") or "All payment dates",
        _filters([
            ("Search", reports.param_text(params, "search")),
            ("Method", reports.param_text(params, "method", 60)),
            ("Service", _service_label(params)),
            ("Order payment status", reports.param_text(params, "payment_status", 40)),
            ("Payment date", _range_text(params, "date_from", "date_to")),
        ]),
        [("Payments", len(rows), "int"), ("Amount received", reports.money(sum(row["amount"] for row in rows)), "money")],
        [Section("Payments", columns, rows, {
            "received_date": f"Total ({len(rows)} payments)",
            "amount": reports.money(sum(row["amount"] for row in rows)),
        }, "Payments")],
    )


BUILDERS = {
    "completed": build_completed,
    "service": build_service,
    "customers": build_customers,
    "customer": build_customer,
    "summary": build_summary,
    "revenue": build_revenue,
    "orders": build_orders,
    "payments": build_payments,
}


# ================================================================== value formatting


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def display(value: Any, kind: str) -> str:
    if value is None or value == "":
        return "—" if kind in {"date", "money", "days"} else ""
    if kind == "money" and isinstance(value, (int, float)):
        return f"PHP {value:,.2f}"
    if kind == "date":
        parsed = _as_date(value)
        return _date_text(parsed) if parsed else str(value)
    if kind == "int" and isinstance(value, (int, float)):
        return f"{int(value):,}"
    if kind == "days" and isinstance(value, (int, float)):
        return f"{value:g} day{'s' if value != 1 else ''}"
    if kind == "percent" and isinstance(value, (int, float)):
        return f"{value:.1f}%"
    return str(value)


def generated_at() -> datetime:
    return datetime.now(SHOP_TIMEZONE)


# ================================================================== Excel


def _sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]\*\?/\\:]", "-", name).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate, counter = cleaned, 2
    while candidate.lower() in used:
        suffix = f" ({counter})"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        counter += 1
    used.add(candidate.lower())
    return candidate


def render_xlsx(report: Report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    money_format = '"₱"#,##0.00'
    date_format = "mmm d, yyyy"
    header_fill = PatternFill("solid", fgColor="2B2B2B")
    total_fill = PatternFill("solid", fgColor="E8E8E8")
    thin = Side(style="thin", color="B3B3B3")

    workbook = Workbook()
    summary = workbook.active
    used: set[str] = set()
    summary.title = _sheet_name("Summary", used)
    stamp = generated_at()
    summary["A1"] = BUSINESS_NAME
    summary["A1"].font = Font(bold=True, size=14)
    summary["A2"] = report.title
    summary["A2"].font = Font(bold=True, size=12)
    summary["A3"], summary["B3"] = "Date generated", stamp.replace(tzinfo=None)
    summary["B3"].number_format = "mmm d, yyyy h:mm AM/PM"
    summary["A4"], summary["B4"] = "Report period", report.period
    row = 6
    summary.cell(row=row, column=1, value="Applied filters").font = Font(bold=True)
    row += 1
    for label, value in report.filters:
        summary.cell(row=row, column=1, value=label)
        summary.cell(row=row, column=2, value=value)
        row += 1
    row += 1
    summary.cell(row=row, column=1, value="Summary").font = Font(bold=True)
    row += 1
    for label, value, kind in report.summary:
        summary.cell(row=row, column=1, value=label)
        cell = summary.cell(row=row, column=2, value=_excel_value(value, kind))
        _excel_format(cell, kind, money_format, date_format)
        row += 1
    summary.column_dimensions["A"].width = 46
    summary.column_dimensions["B"].width = 60

    for section in report.sections:
        sheet = workbook.create_sheet(_sheet_name(section.sheet or section.title, used))
        sheet["A1"] = f"{BUSINESS_NAME} — {section.title}"
        sheet["A1"].font = Font(bold=True, size=12)
        sheet["A2"] = f"{report.title} · {report.period} · generated {stamp:%b %d, %Y %I:%M %p}"
        header_row = 4
        for index, column in enumerate(section.columns, start=1):
            cell = sheet.cell(row=header_row, column=index, value=column.label)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = Border(bottom=thin)
            width = 40 if column.key == "description" else 14 if column.kind in {"money", "date", "int", "days", "percent"} else 22
            sheet.column_dimensions[get_column_letter(index)].width = width
        current = header_row + 1
        for record in section.rows:
            for index, column in enumerate(section.columns, start=1):
                cell = sheet.cell(row=current, column=index, value=_excel_value(record.get(column.key), column.kind))
                _excel_format(cell, column.kind, money_format, date_format)
            current += 1
        if not section.rows:
            sheet.cell(row=current, column=1, value=section.note or "No records match the selected filters.")
            current += 1
        if section.totals:
            for index, column in enumerate(section.columns, start=1):
                value = section.totals.get(column.key)
                cell = sheet.cell(row=current, column=index, value=_excel_value(value, column.kind) if value is not None else None)
                cell.font = Font(bold=True)
                cell.fill = total_fill
                cell.border = Border(top=thin)
                _excel_format(cell, column.kind, money_format, date_format)
        sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
        if section.rows:
            sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(section.columns))}{header_row + len(section.rows)}"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _excel_value(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    if kind == "date":
        parsed = _as_date(value)
        return parsed if parsed else value
    if kind == "percent" and isinstance(value, (int, float)):
        return value / 100
    return value


def _excel_format(cell: Any, kind: str, money_format: str, date_format: str) -> None:
    if kind == "money" and isinstance(cell.value, (int, float)):
        cell.number_format = money_format
    elif kind == "date" and isinstance(cell.value, date):
        cell.number_format = date_format
    elif kind == "percent" and isinstance(cell.value, (int, float)):
        cell.number_format = "0.0%"
    elif kind == "days" and isinstance(cell.value, (int, float)):
        cell.number_format = "0.0" if isinstance(cell.value, float) and not float(cell.value).is_integer() else "0"


# ================================================================== PDF


def render_pdf(report: Report) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    stamp = generated_at()
    styles = getSampleStyleSheet()
    base = ParagraphStyle("cell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.6, leading=9.4)
    right = ParagraphStyle("cell-right", parent=base, alignment=TA_RIGHT)
    head = ParagraphStyle("head", parent=base, fontName="Helvetica-Bold", textColor=colors.white)
    head_right = ParagraphStyle("head-right", parent=head, alignment=TA_RIGHT)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    bold_right = ParagraphStyle("bold-right", parent=bold, alignment=TA_RIGHT)
    title = ParagraphStyle("title", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=17, leading=21, alignment=0, spaceAfter=2)
    business = ParagraphStyle("business", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=13, textColor=colors.HexColor("#555555"))
    meta = ParagraphStyle("meta", parent=base, fontSize=8.6, leading=11.5)
    section_title = ParagraphStyle("section", parent=styles["Heading3"], fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=8, spaceAfter=5)

    page_size = landscape(A4)
    margin = 13 * mm
    usable = page_size[0] - 2 * margin
    numeric = {"money", "int", "days", "percent"}

    class NumberedCanvas(pdf_canvas.Canvas):
        """Adds "Page X of Y" and the report name to every page."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._saved: list[dict[str, Any]] = []

        def showPage(self) -> None:  # noqa: N802 - ReportLab API
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont("Helvetica", 7.5)
                self.setFillColor(colors.HexColor("#555555"))
                self.drawString(margin, 8 * mm, f"{BUSINESS_NAME} · {report.title} · Generated {stamp:%b %d, %Y %I:%M %p}")
                self.drawRightString(page_size[0] - margin, 8 * mm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    def para(text: Any, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(str(text)), style)

    story: list[Any] = [
        para(BUSINESS_NAME, business),
        para(report.title, title),
        Paragraph(
            f"<b>Date generated:</b> {escape(f'{stamp:%B %d, %Y %I:%M %p}')} (Philippine time) &nbsp;&nbsp; "
            f"<b>Report period:</b> {escape(report.period)}",
            meta,
        ),
        Paragraph("<b>Applied filters:</b> " + escape("; ".join(f"{label}: {value}" for label, value in report.filters)), meta),
        Spacer(1, 6),
    ]
    summary_rows = [[para(label, base), para(display(value, kind), bold_right)] for label, value, kind in report.summary]
    if summary_rows:
        summary_table = Table(summary_rows, colWidths=[usable * 0.34, usable * 0.16], hAlign="LEFT")
        summary_table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#B3B3B3")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D4D4D4")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F6F6F6")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story += [Paragraph("Summary", section_title), summary_table]

    for section in report.sections:
        weights = [column.width for column in section.columns]
        widths = [usable * weight / sum(weights) for weight in weights]
        data: list[list[Any]] = [[para(column.label, head_right if column.kind in numeric else head) for column in section.columns]]
        for record in section.rows:
            data.append([
                para(display(record.get(column.key), column.kind), right if column.kind in numeric else base)
                for column in section.columns
            ])
        empty = not section.rows
        if empty:
            data.append([para(section.note or "No records match the selected filters.", base)] + [""] * (len(section.columns) - 1))
        if section.totals:
            data.append([
                para(display(section.totals.get(column.key), column.kind) if section.totals.get(column.key) is not None else "", bold_right if column.kind in numeric else bold)
                for column in section.columns
            ])
        table = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B2B2B")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#D4D4D4")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#B3B3B3")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        if empty:
            style.append(("SPAN", (0, 1), (-1, 1)))
        if section.totals:
            style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8E8E8")), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#2B2B2B"))]
        table.setStyle(TableStyle(style))
        story.append(KeepTogether([Paragraph(escape(section.title), section_title)]))
        story.append(table)

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=15 * mm,
        title=f"{BUSINESS_NAME} — {report.title}",
        author=BUSINESS_NAME,
    )
    document.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


# ================================================================== entry point


def export(kind: str, params: Mapping[str, Any]) -> Response:
    builder = BUILDERS.get(kind)
    if builder is None:
        raise HTTPException(status_code=404, detail="Unknown report.")
    file_format = str(params.get("format") or "pdf").lower()
    if file_format not in FORMATS:
        raise reports.bad_request("Choose PDF or Excel (xlsx) format.")
    query = {key: value for key, value in params.items() if key not in {"format", "page", "page_size"}}
    report = builder(query)
    filename = f"impression-printshop-{report.slug}-{generated_at():%Y-%m-%d}.{file_format}"
    if file_format == "pdf":
        content, media = render_pdf(report), "application/pdf"
    else:
        content, media = render_xlsx(report), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(
        content,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
