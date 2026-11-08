"""Admin reporting, revenue rules, order-action safety, and exports."""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-report-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
# Never read the developer's .env file (SMTP credentials, production mode) during tests.
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.security import create_session_token, SESSION_COOKIE  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import customer_repository, order_repository  # noqa: E402
from backend.app.services import export_services, order_services, report_services  # noqa: E402


WEBP_1X1 = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")


def make_customer(name: str = "Report Customer") -> dict:
    return customer_repository.create(
        {"email": f"{uuid.uuid4().hex[:10]}@example.test", "name": name, "phone": "+63 917 000 0000", "password_hash": "x"},
        "2026-10-01T00:00:00+00:00",
    )


def make_order(
    customer: dict,
    *,
    created_at: str,
    status: str = "Pending Confirmation",
    total: float | None = None,
    payments: list[float] | None = None,
    completed_at: str | None = None,
    category_id: str | None = "souvenir",
    category: str = "Souvenir",
) -> str:
    order = {
        "reference": "",
        "created_at": created_at,
        "updated_at": created_at,
        "customer": {"name": customer["name"], "email": customer["email"], "phone": customer["phone"], "messenger": ""},
        "product": {"id": "white-mug", "category_id": category_id, "name": "White Mug", "category": category, "description": "", "image": ""},
        "customization": {"quantity": 2, "specifications": [{"key": "size", "label": "Size", "value": "11 oz", "type": "select"}], "notes": ""},
        "event_details": {},
        "has_event": False,
        "status": "Pending Confirmation",
        "attachments": [],
        "history": [{"at": created_at, "text": "Customer submitted an order request."}],
        "customer_id": customer["id"],
    }
    if category_id is None:
        order["product"].pop("category_id")
    reference = order_repository.create_order(order, f"test-{uuid.uuid4().hex}")
    with connection_scope() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, total_amount = ?, payments = ?, completed_at = ? WHERE reference = ?",
            (
                status,
                total,
                json.dumps([{"amount": amount, "method": "Cash", "reference": "", "received_at": created_at} for amount in (payments or [])]),
                completed_at,
                reference,
            ),
        )
    return reference


class ReportCalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def test_completion_duration_counts_calendar_days_including_weekends(self) -> None:
        customer = make_customer()
        # Nov 1 2026 09:00 Manila → Nov 5 2026 10:00 Manila (Nov 1 is a Sunday): 4 calendar days.
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=500, payments=[500], completed_at="2026-11-05T02:00:00+00:00")
        row = report_services.order_row(order_repository.get_order(reference), report_services.ServiceIndex())
        self.assertEqual(row["request_date"], "2026-11-01")
        self.assertEqual(row["completion_date"], "2026-11-05")
        self.assertEqual(row["completion_days"], 4)

    def test_dates_use_shop_time_zone(self) -> None:
        customer = make_customer()
        # 2026-11-02 17:30 UTC is already Nov 3 in Manila.
        reference = make_order(customer, created_at="2026-11-02T17:30:00+00:00", status="Completed", total=100, payments=[100], completed_at="2026-11-03T03:00:00+00:00")
        row = report_services.order_row(order_repository.get_order(reference), report_services.ServiceIndex())
        self.assertEqual(row["request_date"], "2026-11-03")
        self.assertEqual(row["completion_days"], 0)

    def test_missing_completion_timestamp_gives_no_duration(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=100, payments=[100], completed_at=None)
        row = report_services.order_row(order_repository.get_order(reference), report_services.ServiceIndex())
        self.assertIsNone(row["completion_days"])

    def test_revenue_counts_only_completed_and_fully_paid_orders_once(self) -> None:
        customer = make_customer("Revenue Customer")
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=1000, payments=[500, 500], completed_at="2026-11-02T01:00:00+00:00")
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=800, payments=[300], completed_at="2026-11-02T01:00:00+00:00")
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Ready for Pickup", total=600, payments=[600])
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Cancelled")
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Awaiting Down Payment", total=400)
        detail = report_services.customer_detail(customer["id"])
        self.assertEqual(detail["customer"]["total_spent"], 1000.0)
        self.assertEqual(detail["customer"]["completed_orders"], 2)
        self.assertEqual(detail["customer"]["pending_orders"], 2)
        completed = report_services.completed_list({"customer": customer["email"]})
        self.assertEqual(completed["summary"]["confirmed_revenue"], 1000.0)
        self.assertEqual(completed["summary"]["completed_orders"], 2)
        self.assertEqual(completed["summary"]["fully_paid_orders"], 1)

    def test_dashboard_revenue_matches_customer_spend_and_reports(self) -> None:
        dashboard = report_services.dashboard()
        spend = report_services.customer_list({"page_size": "all"})["summary"]["total_spent"]
        summary = report_services.business_summary({"preset": "all"})
        self.assertAlmostEqual(dashboard["total_revenue"], summary["totals"]["confirmed_revenue"], places=2)
        self.assertAlmostEqual(sum(item["revenue"] for item in summary["by_service"]), summary["totals"]["confirmed_revenue"], places=2)
        self.assertAlmostEqual(sum(bucket["revenue"] for bucket in summary["trend"]), summary["totals"]["confirmed_revenue"], places=2)
        # Orders without an account are not customer spend, so spend can only be equal or lower.
        self.assertLessEqual(spend, dashboard["total_revenue"] + 0.001)

    def test_legacy_category_labels_map_to_services(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00", category_id=None, category="Sticker Printing")
        row = report_services.order_row(order_repository.get_order(reference), report_services.ServiceIndex())
        self.assertEqual(row["service_id"], "sticker")

    def test_service_report_never_mixes_services(self) -> None:
        customer = make_customer()
        make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=50, payments=[50], completed_at="2026-11-01T02:00:00+00:00", category_id="invitation", category="Invitation")
        report = report_services.service_report({"service": "invitation", "page_size": "all"})
        self.assertTrue(report["items"])
        self.assertTrue(all(row["service_id"] == "invitation" for row in report["items"]))

    def test_completed_filters(self) -> None:
        customer = make_customer("Filter Customer")
        make_order(customer, created_at="2026-10-01T01:00:00+00:00", status="Completed", total=100, payments=[100], completed_at="2026-10-11T01:00:00+00:00")
        make_order(customer, created_at="2026-10-05T01:00:00+00:00", status="Completed", total=100, payments=[50], completed_at="2026-10-06T01:00:00+00:00")
        base = {"customer": customer["email"]}
        self.assertEqual(report_services.completed_list({**base, "min_days": "5"})["total"], 1)
        self.assertEqual(report_services.completed_list({**base, "max_days": "1"})["total"], 1)
        self.assertEqual(report_services.completed_list({**base, "payment_status": "Partially Paid"})["total"], 1)
        self.assertEqual(report_services.completed_list({**base, "completed_from": "2026-10-10", "completed_to": "2026-10-31"})["total"], 1)
        self.assertEqual(report_services.completed_list({**base, "request_from": "2026-10-02"})["total"], 1)
        with self.assertRaises(HTTPException):
            report_services.completed_list({**base, "min_days": "5", "max_days": "1"})

    def test_pagination_and_sorting(self) -> None:
        result = report_services.order_list({"page_size": "10", "page": "1", "sort": "reference", "dir": "asc"})
        references = [row["reference"] for row in result["items"]]
        self.assertEqual(references, sorted(references, key=str.casefold))
        self.assertLessEqual(len(references), 10)
        self.assertEqual(result["pages"], max(1, -(-result["total"] // 10)))

    def test_period_buckets(self) -> None:
        week = report_services.business_summary({"preset": "custom", "date_from": "2026-11-02", "date_to": "2026-11-15", "group_by": "week"})
        self.assertEqual([bucket["start"] for bucket in week["trend"]], ["2026-11-02", "2026-11-09"])
        with self.assertRaises(HTTPException):
            report_services.business_summary({"preset": "custom", "date_from": "2026-11-02"})


class OrderActionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def test_completion_requires_full_payment_and_sets_completed_at(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Ready for Pickup", total=1000, payments=[500])
        with self.assertRaises(HTTPException) as raised:
            order_services.update_order_status(reference, "Completed")
        self.assertEqual(raised.exception.status_code, 409)
        order_services.record_payment(reference, {"amount": 500, "method": "Cash", "received": True})
        completed = order_services.update_order_status(reference, "Completed")
        self.assertEqual(completed["status"], "Completed")
        self.assertTrue(order_repository.get_order(reference)["completed_at"])

    def test_stale_version_is_refused(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00")
        version = order_repository.order_version(order_repository.get_order(reference))
        order_services.update_order_status(reference, "Under Review", {"expected_version": version})
        with self.assertRaises(HTTPException) as raised:
            order_services.update_order_status(reference, "Awaiting Quotation", {"expected_version": version})
        self.assertEqual(raised.exception.status_code, 409)

    def test_rejection_reason_is_recorded(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00")
        result = order_services.update_order_status(reference, "Rejected", {"note": "Artwork resolution too low"})
        self.assertIn("Artwork resolution too low", result["history"][-1]["text"])
        self.assertEqual(result["admin_notes"], "Artwork resolution too low")

    def test_completed_at_backfilled_from_history(self) -> None:
        customer = make_customer()
        reference = make_order(customer, created_at="2026-11-01T01:00:00+00:00", status="Completed", total=10, payments=[10])
        with connection_scope() as connection:
            connection.execute(
                "UPDATE orders SET history = ?, completed_at = NULL WHERE reference = ?",
                (json.dumps([{"at": "2026-11-01T01:00:00+00:00", "text": "x"}, {"at": "2026-11-04T05:00:00+00:00", "text": "Administrator updated the order status to Completed."}]), reference),
            )
        order_repository.initialize()
        self.assertEqual(order_repository.get_order(reference)["completed_at"], "2026-11-04T05:00:00+00:00")

    def test_webp_signature_is_validated(self) -> None:
        metadata, _ = order_services._decode_upload({"name": "logo.webp", "dataUrl": "data:image/webp;base64," + base64.b64encode(WEBP_1X1).decode()})
        self.assertEqual(metadata["mime_type"], "image/webp")
        with self.assertRaises(HTTPException):
            order_services._decode_upload({"name": "fake.webp", "dataUrl": "data:image/webp;base64," + base64.b64encode(b"RIFF0000NOPE0000").decode()})


class AdminEndpointAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def test_admin_endpoints_require_admin_session(self) -> None:
        for path in ("/api/admin/customers", "/api/admin/payments", "/api/admin/reports/completed", "/api/admin/reports/services",
                     "/api/admin/reports/summary", "/api/admin/activity", "/api/admin/exports/completed?format=pdf"):
            self.assertEqual(TestClient(app).get(path).status_code, 401, path)
        customer_client = TestClient(app)
        customer_client.cookies.set(SESSION_COOKIE, create_session_token(make_customer()["id"], "customer"))
        self.assertEqual(customer_client.get("/api/admin/reports/summary").status_code, 401)

    def test_exports_produce_pdf_and_excel_with_service_sheets(self) -> None:
        admin = TestClient(app)
        admin.cookies.set(SESSION_COOKIE, create_session_token(settings.admin_email or "admin@example.test", "admin"))
        pdf = admin.get("/api/admin/exports/summary", params={"format": "pdf", "preset": "all"})
        self.assertEqual(pdf.status_code, 200, pdf.text)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        xlsx = admin.get("/api/admin/exports/service", params={"format": "xlsx", "service": "all"})
        self.assertEqual(xlsx.status_code, 200, xlsx.text)
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(xlsx.content))
        self.assertIn("Summary", workbook.sheetnames)
        self.assertIn("Overview", workbook.sheetnames)
        self.assertIn("Invitation", workbook.sheetnames)
        self.assertIn("Souvenir", workbook.sheetnames)
        self.assertEqual(admin.get("/api/admin/exports/unknown", params={"format": "pdf"}).status_code, 404)
        self.assertEqual(admin.get("/api/admin/exports/completed", params={"format": "docx"}).status_code, 422)

    def test_export_rows_match_screen_filters(self) -> None:
        params = {"service": "souvenir", "payment_status": "Fully Paid"}
        rows, _ = report_services.completed_rows(params)
        report = export_services.build_completed(params)
        self.assertEqual([row["reference"] for row in report.sections[0].rows], [row["reference"] for row in rows])
        self.assertEqual(report.sections[0].totals["total_amount"], round(sum(row["total_amount"] for row in rows), 2))


if __name__ == "__main__":
    unittest.main()
