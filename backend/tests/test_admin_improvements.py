"""Password policy, payment references and verification, customer payment views, notifications, and
database backup/restore."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any


_test_directory = tempfile.TemporaryDirectory(prefix="impress-admin-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import admin_repository, customer_repository, order_repository  # noqa: E402
from backend.app.schemas.user import password_problems  # noqa: E402
from backend.app.services import backup_services, order_services, report_services  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


def make_customer(name: str = "Admin Test Customer") -> dict:
    return customer_repository.create(
        {"email": f"{uuid.uuid4().hex[:10]}@example.test", "name": name, "phone": "+63 917 000 0000", "password_hash": "x"},
        "2026-10-01T00:00:00+00:00",
    )


def make_order(customer: dict, *, status: str, total: float | None, payments: list[dict] | None = None, submissions: list[dict] | None = None) -> str:
    created_at = "2026-11-01T01:00:00+00:00"
    order = {
        "reference": "",
        "created_at": created_at,
        "updated_at": created_at,
        "customer": {"name": customer["name"], "email": customer["email"], "phone": customer["phone"], "messenger": ""},
        "product": {"id": "white-mug", "category_id": "souvenir", "name": "White Mug", "category": "Souvenir", "description": "", "image": ""},
        "customization": {"quantity": 2, "notes": ""},
        "event_details": {},
        "has_event": False,
        "status": "Pending Confirmation",
        "attachments": [],
        "history": [{"at": created_at, "text": "Customer submitted an order request."}],
        "customer_id": customer["id"],
        "payment_submissions": submissions or [],
    }
    reference = order_repository.create_order(order, f"test-{uuid.uuid4().hex}")
    with connection_scope() as connection:
        connection.execute(
            "UPDATE orders SET status = ?, total_amount = ?, payments = ? WHERE reference = ?",
            (status, total, json.dumps(payments or []), reference),
        )
    return reference


def admin_client() -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        SESSION_COOKIE,
        create_session_token(settings.admin_email or "admin@example.test", "admin", session_version=admin_repository.get_session_version()),
    )
    return client


class AdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)


class PasswordPolicyTests(AdminTestCase):
    def test_rules(self) -> None:
        self.assertEqual(password_problems("abc12345"), [])
        self.assertTrue(any("8 characters" in item for item in password_problems("ab12")))
        self.assertTrue(any("letter" in item for item in password_problems("12345678")))
        self.assertTrue(any("number" in item for item in password_problems("abcdefgh")))
        self.assertTrue(any("spaces" in item for item in password_problems("abc 12345")))

    def test_registration_enforces_rules_on_the_server(self) -> None:
        for password in ("short1", "onlyletters", "1234567890", "has space 123"):
            response = self.client.post("/api/customer/register", json={
                "name": "Rule Tester", "email": f"{uuid.uuid4().hex[:8]}@example.test", "phone": "09171234567", "password": password,
            })
            self.assertEqual(response.status_code, 422, (password, response.text))
        accepted = self.client.post("/api/customer/register", json={
            "name": "Rule Tester", "email": f"{uuid.uuid4().hex[:8]}@example.test", "phone": "09171234567", "password": "printing8",
        })
        self.assertEqual(accepted.status_code, 201, accepted.text)

    def test_registration_page_lists_every_requirement(self) -> None:
        page = self.client.get("/create-account").text
        for rule in ("length", "letter", "number", "spaces", "match"):
            self.assertIn(f'data-rule="{rule}"', page)
        self.assertIn('data-toggle-password="register-password"', page)


class PaymentReferenceTests(AdminTestCase):
    def test_provider_payments_require_a_reference(self) -> None:
        reference = make_order(make_customer(), status="Awaiting Down Payment", total=1000)
        with self.assertRaises(HTTPException) as caught:
            order_services.record_payment(reference, {"amount": 500, "method": "GCash", "received": True})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(stored_order(reference)["payments"], [])
        view = order_services.record_payment(reference, {"amount": 500, "method": "GCash", "reference": "GC-9001-2026", "received": True})
        payment = view["payments"][0]
        self.assertEqual(payment["reference"], "GC-9001-2026")
        self.assertEqual(payment["reference_type"], "transaction")
        self.assertEqual(report_services.payment_status(stored_order(reference)), "Partially Paid")
        self.assertEqual(view["status"], "Confirmed")
        # A reference can be used once across all orders.
        other = make_order(make_customer(), status="Awaiting Down Payment", total=600)
        with self.assertRaises(HTTPException) as reused:
            order_services.record_payment(other, {"amount": 300, "method": "Maya", "reference": "gc 9001 2026", "received": True})
        self.assertEqual(reused.exception.status_code, 409)

    def test_cash_without_receipt_gets_a_system_receipt_and_verification_record(self) -> None:
        reference = make_order(make_customer(), status="Ready for Pickup", total=400)
        view = order_services.record_payment(reference, {"amount": 400, "method": "Cash", "received": True, "verification_note": "Received by Ana"}, actor="owner@example.test")
        payment = view["payments"][0]
        self.assertTrue(payment["reference"].startswith("CASH-"))
        self.assertEqual(payment["reference_type"], "system_cash_receipt")
        self.assertEqual(payment["verification_view"]["status"], "Verified")
        self.assertEqual(payment["verification_view"]["verified_by"], "owner@example.test")
        row = report_services.order_row(stored_order(reference), report_services.ServiceIndex())
        self.assertEqual((row["payment_status"], row["balance"]), ("Fully Paid", 0))

    def test_status_change_never_marks_an_order_paid(self) -> None:
        reference = make_order(make_customer(), status="Ready for Pickup", total=700)
        with self.assertRaises(HTTPException):
            order_services.update_order_status(reference, "Completed")
        self.assertEqual(report_services.payment_status(stored_order(reference)), "Unpaid")

    def test_customer_views_hide_who_verified_payments(self) -> None:
        reference = make_order(make_customer(), status="Ready for Pickup", total=400)
        order_services.record_payment(reference, {"amount": 100, "method": "Cash", "received": True}, actor="owner@example.test")
        customer_view = order_services.order_view(stored_order(reference))
        self.assertNotIn("recorded_by", customer_view["payments"][0])
        self.assertNotIn("verification", customer_view["payments"][0])

    def test_missing_reference_is_flagged_and_can_be_added_once(self) -> None:
        legacy = [{"amount": 300, "method": "GCash", "reference": "", "received_at": "2026-11-02T01:00:00+00:00"}]
        reference = make_order(make_customer(), status="Ready for Pickup", total=300, payments=legacy)
        rows = report_services.payment_list({"search": reference, "verification": "missing"})
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["items"][0]["verification_status"], "Reference Missing")
        admin = admin_client()
        refused = admin.post(f"/api/admin/orders/{reference}/payments/1/reference", json={"reference": "GC-LEGACY-777"})
        self.assertEqual(refused.status_code, 422, refused.text)
        saved = admin.post(f"/api/admin/orders/{reference}/payments/1/reference", json={"reference": "GC-LEGACY-777", "confirmed": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        payment = saved.json()["payments"][0]
        self.assertEqual((payment["reference"], payment["amount"]), ("GC-LEGACY-777", 300))
        again = admin.post(f"/api/admin/orders/{reference}/payments/1/reference", json={"reference": "GC-OTHER-1", "confirmed": True})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(report_services.payment_list({"search": reference, "verification": "missing"})["total"], 0)

    def test_transactions_list_submissions_once_and_count_only_verified_money(self) -> None:
        submissions = [
            {"id": "sub-pending", "amount": 250, "method": "GCash", "reference": "PEND-1234", "submitted_at": "2026-11-03T01:00:00+00:00", "status": "pending", "stage": "down_payment"},
            {"id": "sub-rejected", "amount": 250, "method": "GCash", "reference": "REJ-1234", "submitted_at": "2026-11-02T01:00:00+00:00", "status": "rejected", "review_note": "Not found"},
            {"id": "sub-verified", "amount": 250, "method": "GCash", "reference": "VER-1234", "submitted_at": "2026-11-01T02:00:00+00:00", "status": "verified"},
        ]
        payments = [{"amount": 250, "method": "GCash", "reference": "VER-1234", "received_at": "2026-11-01T03:00:00+00:00", "source": "customer submission", "submission_id": "sub-verified"}]
        reference = make_order(make_customer(), status="Confirmed", total=1000, payments=payments, submissions=submissions)
        result = report_services.payment_list({"search": reference})
        statuses = sorted(row["verification_status"] for row in result["items"])
        self.assertEqual(statuses, ["Pending Verification", "Rejected", "Verified"])
        self.assertEqual(result["summary"]["filtered_amount"], 250)
        self.assertEqual(result["summary"]["filtered_count"], 1)
        pending = report_services.payment_list({"search": reference, "verification": "pending"})
        self.assertEqual([row["payment_reference"] for row in pending["items"]], ["PEND-1234"])


class CustomerViewTests(AdminTestCase):
    def test_paid_and_unpaid_views_never_overlap(self) -> None:
        paid = make_customer("View Paid")
        make_order(paid, status="Completed", total=500, payments=[{"amount": 500, "method": "Cash", "reference": "OR-1001", "received_at": "2026-11-02T01:00:00+00:00"}])
        unpaid = make_customer("View Unpaid")
        make_order(unpaid, status="Ready for Pickup", total=500, payments=[{"amount": 200, "method": "Cash", "reference": "OR-1002", "received_at": "2026-11-02T01:00:00+00:00"}])
        quiet = make_customer("View Quiet")
        ids = {}
        for view in ("all", "paid", "unpaid"):
            result = report_services.customer_list({"view": view, "page_size": "all"})
            ids[view] = {row["id"] for row in result["items"]}
            self.assertEqual(result["view_counts"][view], len(ids[view]))
        self.assertIn(paid["id"], ids["paid"])
        self.assertIn(unpaid["id"], ids["unpaid"])
        self.assertTrue(ids["paid"].isdisjoint(ids["unpaid"]))
        self.assertNotIn(quiet["id"], ids["paid"] | ids["unpaid"])
        self.assertIn(quiet["id"], ids["all"])
        with self.assertRaises(HTTPException):
            report_services.customer_list({"view": "everyone"})

    def test_print_report_includes_history_and_payment_references(self) -> None:
        customer = make_customer("Print Me")
        make_order(customer, status="Ready for Pickup", total=500, payments=[{"amount": 200, "method": "GCash", "reference": "GC-PRINT-1", "received_at": "2026-11-02T01:00:00+00:00"}])
        response = admin_client().get("/api/admin/customers-report", params={"customer_id": customer["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["view"], "single")
        entry = data["customers"][0]
        self.assertEqual(entry["payments"][0]["payment_reference"], "GC-PRINT-1")
        self.assertEqual(entry["orders"][0]["balance"], 300)
        listing = admin_client().get("/api/admin/customers-report", params={"view": "unpaid", "search": "Print Me"}).json()
        self.assertEqual([row["name"] for row in listing["customers"]], ["Print Me"])
        self.assertNotIn("orders", listing["customers"][0])
        self.assertEqual(TestClient(app).get("/api/admin/customers-report").status_code, 401)


class NotificationTests(AdminTestCase):
    def test_pagination_filters_and_read_state(self) -> None:
        customer = make_customer("Notify Me")
        reference = make_order(customer, status="Pending Confirmation", total=None)
        admin = admin_client()
        page = admin.get("/api/admin/notifications", params={"page_size": 10, "search": reference}).json()
        self.assertEqual(page["total"], 1)
        item = page["items"][0]
        self.assertFalse(item["read"])
        self.assertEqual(item["category"], "orders")
        unread_before = page["unread_count"]
        marked = admin.post("/api/admin/notifications/read", json={"ids": [item["id"]]})
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(marked.json()["unread_count"], unread_before - 1)
        self.assertTrue(admin.get("/api/admin/notifications", params={"search": reference}).json()["items"][0]["read"])
        self.assertEqual(admin.get("/api/admin/notifications", params={"search": reference, "status": "unread"}).json()["total"], 0)
        paged = admin.get("/api/admin/notifications", params={"page_size": 10, "page": 1}).json()
        self.assertLessEqual(len(paged["items"]), 10)
        self.assertEqual(paged["page_size"], 10)
        admin.post("/api/admin/notifications/read", json={"all": True})
        self.assertEqual(admin.get("/api/admin/notifications/recent").json()["unread_count"], 0)
        self.assertEqual(report_services.action_counts()["unread_notifications"], 0)
        self.assertEqual(admin.get("/api/admin/notifications", params={"status": "bogus"}).status_code, 422)
        self.assertEqual(TestClient(app).get("/api/admin/notifications").status_code, 401)


class BackupRestoreTests(AdminTestCase):
    def test_backup_and_restore_round_trip(self) -> None:
        admin = admin_client()
        customer = make_customer("Backup Customer")
        kept = make_order(customer, status="Ready for Pickup", total=500, payments=[{"amount": 500, "method": "Cash", "reference": "OR-BK-1", "received_at": "2026-11-02T01:00:00+00:00"}])
        response = admin.post("/api/admin/backup")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment;", response.headers["content-disposition"])
        backup = response.json()
        self.assertNotIn("admin_credentials", backup["tables"])
        self.assertNotIn("customer_password_resets", backup["tables"])
        # Data added after the backup disappears on restore; data in the backup comes back.
        later = make_order(make_customer("After Backup"), status="Pending Confirmation", total=None)
        with connection_scope() as connection:
            connection.execute("DELETE FROM orders WHERE reference = ?", (kept,))
        status = admin.get("/api/admin/backup").json()
        self.assertEqual(status["last_backup"]["type"], "backup")
        restored = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": backup})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIsNotNone(order_repository.get_order(kept))
        self.assertIsNone(order_repository.get_order(later))
        self.assertEqual(stored_order(kept)["payments"][0]["reference"], "OR-BK-1")
        history = admin.get("/api/admin/backup").json()["history"]
        self.assertEqual(history[0]["type"], "restore")
        self.assertEqual(history[0]["status"], "success")

    def test_restore_accepts_the_file_text_and_reencoded_json(self) -> None:
        admin = admin_client()
        make_order(make_customer("Float Check"), status="Ready for Pickup", total=450.0,
                   payments=[{"amount": 450.0, "method": "Cash", "reference": "OR-FLOAT-1", "received_at": "2026-11-02T01:00:00+00:00"}])
        text = admin.post("/api/admin/backup").text
        as_text = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup_text": text})
        self.assertEqual(as_text.status_code, 200, as_text.text)
        # A browser's JSON.parse turns 450.0 into 450; the checksum treats them alike.
        def like_javascript(value: Any) -> Any:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, list):
                return [like_javascript(item) for item in value]
            if isinstance(value, dict):
                return {key: like_javascript(item) for key, item in value.items()}
            return value

        reencoded = like_javascript(json.loads(text))
        self.assertIn(450, [cell for row in reencoded["tables"]["orders"]["rows"] for cell in row])
        again = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": reencoded})
        self.assertEqual(again.status_code, 200, again.text)

    def test_restore_rejects_bad_files_without_changing_data(self) -> None:
        admin = admin_client()
        backup = admin.post("/api/admin/backup").json()
        marker = make_order(make_customer("Must Survive"), status="Pending Confirmation", total=None)
        tampered = copy.deepcopy(backup)
        tampered["tables"]["orders"]["rows"] = []
        resigned_elsewhere = copy.deepcopy(backup)
        resigned_elsewhere["signature"] = "0" * 64
        broken_json = copy.deepcopy(backup)
        columns = broken_json["tables"]["orders"]["columns"]
        if broken_json["tables"]["orders"]["rows"]:
            broken_json["tables"]["orders"]["rows"][0][columns.index("payments")] = "{not json"
        for document in (tampered, resigned_elsewhere, {"format": "something-else"}, "text"):
            response = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": document})
            self.assertEqual(response.status_code, 422, response.text)
        # Without the typed confirmation nothing happens either.
        self.assertEqual(admin.post("/api/admin/backup/restore", json={"backup": backup}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/backup/restore", content=b"not json", headers={"Content-Type": "application/json"}).status_code, 422)
        self.assertIsNotNone(order_repository.get_order(marker))
        with self.assertRaises(HTTPException):
            backup_services.validate_backup(broken_json if broken_json["tables"]["orders"]["rows"] else tampered)

    def test_backup_requires_an_administrator(self) -> None:
        anonymous = TestClient(app)
        self.assertEqual(anonymous.post("/api/admin/backup").status_code, 401)
        self.assertEqual(anonymous.get("/api/admin/backup").status_code, 401)
        self.assertEqual(anonymous.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": {}}).status_code, 401)
        customer = TestClient(app)
        customer.cookies.set(SESSION_COOKIE, create_session_token(make_customer()["id"], "customer", session_version=0))
        self.assertEqual(customer.post("/api/admin/backup").status_code, 401)


class TursoBatchTests(unittest.TestCase):
    """The restore sends many statements per request to Turso and stops at the first failure."""

    def test_batch_chunks_and_raises_on_errors(self) -> None:
        import sqlite3
        from unittest.mock import patch

        from backend.app.database.turso import TursoConnection

        connection = TursoConnection("libsql://example.invalid", "token")
        connection.in_transaction = True
        sent: list[int] = []

        def ok(requests):
            sent.append(len(requests))
            return [{"type": "ok", "response": {"result": {"cols": [], "rows": []}}} for _ in requests]

        with patch.object(connection, "_pipeline", side_effect=ok):
            connection.execute_batch([("INSERT INTO t VALUES (?)", [index]) for index in range(450)], chunk_size=200)
        self.assertEqual(sent, [200, 200, 50])

        def failing(requests):
            return [{"type": "error", "error": {"message": "UNIQUE constraint failed", "code": "SQLITE_CONSTRAINT"}}] * len(requests)

        with patch.object(connection, "_pipeline", side_effect=failing), self.assertRaises(sqlite3.IntegrityError):
            connection.execute_batch([("INSERT INTO t VALUES (1)", [])])
        connection.in_transaction = False
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute_batch([("DELETE FROM t", [])])


if __name__ == "__main__":
    unittest.main()
