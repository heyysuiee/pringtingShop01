from __future__ import annotations

import os
import re
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_test_directory = tempfile.TemporaryDirectory(prefix="impress-auth-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.repositories import customer_repository  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import order_repository  # noqa: E402
from backend.app.schemas.user import CustomerRegistration  # noqa: E402
from backend.app.services import auth_services, email_service  # noqa: E402
from backend.app.utils.order_number import base_reference, candidate_reference  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
NEW_PASSWORD = "Brand-New-Password-22"
REFERENCE_PATTERN = re.compile(r"^[A-Z]{3}-\d{2}-\d{2}-\d{2}(-\d+)?$")


def unique_email() -> str:
    return f"{uuid.uuid4().hex[:12]}@example.test"


def order_payload(email: str, phone: str = "+63 917 000 1111") -> dict:
    return {
        "customer": {"name": "Test Customer", "email": email, "phone": phone, "messenger": ""},
        # A seeded catalog product; the server reads its name, fields, and prices from the database.
        "product": {"id": "white-mug"},
        "customization": {
            "quantity": 2,
            "options": {
                "size": "11-oz",
                "print_area": "one-side",
                "inner_color": "white-classic",
                "packaging": "no-packaging",
            },
            "notes": "",
        },
        "event": {},
        "has_event": False,
        "acknowledged": True,
        "files": [],
    }


class AuthFlowTestCase(unittest.TestCase):
    client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()
        self.sent: list[tuple[str, str]] = []

        def capture(recipient: str, subject: str, body: str) -> None:
            del subject  # Same signature as email_service.send_message; the tests only read the body.
            self.sent.append((recipient, body))

        patcher = patch.object(email_service, "send_message", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def new_client(self) -> TestClient:
        return TestClient(app)

    def register(self, client: TestClient | None = None, email: str | None = None) -> dict:
        target = client or self.client
        email = email or unique_email()
        response = target.post(
            "/api/customer/register",
            json={
                "name": "Print Customer",
                "email": email,
                "phone": "+63 917 123 4567",
                "messenger": "",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["customer"]

    def last_code(self) -> str:
        self.assertTrue(self.sent, "No verification email was sent.")
        match = re.search(r"verification code is: (\d{6})", self.sent[-1][1])
        self.assertIsNotNone(match)
        return match.group(1)  # type: ignore[union-attr]


class OrderReferenceTests(AuthFlowTestCase):
    def test_reference_format_uses_prefix_month_day_and_year(self) -> None:
        created = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)  # 09:00 in Manila
        self.assertEqual(base_reference(created), "IPS-11-02-26")
        self.assertEqual(candidate_reference("IPS-11-02-26", 1), "IPS-11-02-26")
        self.assertEqual(candidate_reference("IPS-11-02-26", 2), "IPS-11-02-26-2")

    def test_concurrent_submissions_receive_unique_references(self) -> None:
        customer = auth_services.register_customer(
            CustomerRegistration(
                name="Concurrent Customer",
                email=unique_email(),
                phone="+63 917 555 0000",
                messenger="",
                password=PASSWORD,
            )
        )
        references: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def submit() -> None:
            try:
                order = {
                    "reference": "",
                    "created_at": now,
                    "updated_at": now,
                    "customer": {"name": "C", "email": customer["email"], "phone": customer["phone"], "messenger": ""},
                    "product": {"name": "P", "category": "Test", "description": "", "image": ""},
                    "customization": {"quantity": 1},
                    "event_details": {},
                    "has_event": False,
                    "status": "Pending Confirmation",
                    "attachments": [],
                    "history": [],
                    "customer_id": customer["id"],
                }
                reference = order_repository.create_order(order, f"concurrency-{uuid.uuid4().hex}")
                with lock:
                    references.append(reference)
            except BaseException as error:  # pragma: no cover - surfaced below
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=submit) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(references), 12)
        self.assertEqual(len(set(references)), 12, "Duplicate order references were issued.")
        for reference in references:
            self.assertRegex(reference, REFERENCE_PATTERN)


class OrderGatingTests(AuthFlowTestCase):
    def test_guest_cannot_submit_an_order(self) -> None:
        guest = self.new_client()
        response = guest.post(
            "/api/orders",
            json=order_payload(unique_email()),
            headers={"Idempotency-Key": uuid.uuid4().hex + uuid.uuid4().hex[:8]},
        )
        self.assertEqual(response.status_code, 401)

    def test_signed_in_customer_order_is_linked_and_generated_by_server(self) -> None:
        shopper = self.new_client()
        customer = self.register(shopper)
        response = shopper.post(
            "/api/orders",
            json=order_payload(customer["email"]),
            headers={"Idempotency-Key": uuid.uuid4().hex + uuid.uuid4().hex[:8]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertRegex(body["reference"], REFERENCE_PATTERN)
        self.assertNotIn("customer_id", body)

        history = shopper.get("/api/account/orders").json()["orders"]
        self.assertIn(body["reference"], [order["reference"] for order in history])

    def test_customer_cannot_read_another_customers_order(self) -> None:
        owner = self.new_client()
        owner_account = self.register(owner)
        submitted = owner.post(
            "/api/orders",
            json=order_payload(owner_account["email"]),
            headers={"Idempotency-Key": uuid.uuid4().hex + uuid.uuid4().hex[:8]},
        ).json()

        intruder = self.new_client()
        self.register(intruder)
        self.assertEqual(intruder.get(f"/api/account/orders/{submitted['reference']}").status_code, 404)
        self.assertEqual(owner.get(f"/api/account/orders/{submitted['reference']}").status_code, 200)


class ForgotPasswordTests(AuthFlowTestCase):
    def test_full_reset_flow_changes_password_and_single_use_otp(self) -> None:
        customer = self.register()
        email = customer["email"]
        requested = self.client.post("/api/customer/password-reset/request", json={"email": email})
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(len(self.sent), 1)
        code = self.last_code()

        verified = self.client.post("/api/customer/password-reset/verify", json={"email": email, "code": code})
        self.assertEqual(verified.status_code, 200, verified.text)
        token = verified.json()["reset_token"]

        confirmed = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": email, "reset_token": token, "new_password": NEW_PASSWORD},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

        self.assertIsNone(auth_services.authenticate_customer(email, PASSWORD))
        self.assertIsNotNone(auth_services.authenticate_customer(email, NEW_PASSWORD))

        # Reusing the code or the token must fail.
        reused_code = self.client.post("/api/customer/password-reset/verify", json={"email": email, "code": code})
        self.assertEqual(reused_code.status_code, 422)
        reused_token = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": email, "reset_token": token, "new_password": "Another-Password-99"},
        )
        self.assertEqual(reused_token.status_code, 422)

    def test_wrong_code_is_limited_after_five_attempts(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        real_code = self.last_code()
        wrong = "000000" if real_code != "000000" else "111111"
        for _ in range(4):
            response = self.client.post(
                "/api/customer/password-reset/verify", json={"email": customer["email"], "code": wrong}
            )
            self.assertEqual(response.status_code, 422)
        fifth = self.client.post("/api/customer/password-reset/verify", json={"email": customer["email"], "code": wrong})
        self.assertEqual(fifth.status_code, 429)
        # Even the correct code no longer works once the attempt limit has been reached.
        locked = self.client.post(
            "/api/customer/password-reset/verify", json={"email": customer["email"], "code": real_code}
        )
        self.assertEqual(locked.status_code, 422)

    def test_expired_code_is_rejected(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        code = self.last_code()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        with connection_scope() as connection:
            connection.execute(
                "UPDATE customer_password_resets SET code_expires_at = ? WHERE customer_id = ?",
                (expired, customer["id"]),
            )
        response = self.client.post("/api/customer/password-reset/verify", json={"email": customer["email"], "code": code})
        self.assertEqual(response.status_code, 422)

    def test_unknown_email_gets_the_same_response_and_sends_nothing(self) -> None:
        response = self.client.post("/api/customer/password-reset/request", json={"email": unique_email()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], auth_services.GENERIC_REQUEST_MESSAGE)
        self.assertEqual(self.sent, [])

    def test_resend_is_throttled_by_cooldown(self) -> None:
        customer = self.register()
        first = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        second = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertEqual(first.json()["resend_after"], 60)
        self.assertEqual(second.json()["resend_after"], 60)
        self.assertEqual(len(self.sent), 1, "A resend inside the cooldown must not send another email.")

    def test_email_delivery_failure_is_reported_without_claiming_success(self) -> None:
        customer = self.register()
        with patch.object(email_service, "send_message", side_effect=email_service.EmailDeliveryError("down")):
            response = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(customer_repository.get_reset(customer["id"]))

    def test_short_new_password_is_rejected(self) -> None:
        customer = self.register()
        response = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": "x" * 30, "new_password": "short"},
        )
        self.assertEqual(response.status_code, 422)


class RateLimitTests(AuthFlowTestCase):
    def test_repeated_failed_sign_ins_are_throttled(self) -> None:
        customer = self.register()
        statuses = [
            self.client.post("/api/customer/session", json={"email": customer["email"], "password": "wrong-password-1"}).status_code
            for _ in range(11)
        ]
        self.assertEqual(statuses[:10], [401] * 10)
        self.assertEqual(statuses[10], 429)




class AdminDashboardAccessTests(AuthFlowTestCase):
    def test_dashboard_requires_admin_session(self) -> None:
        anonymous = self.new_client()
        self.assertEqual(anonymous.get("/api/admin/dashboard").status_code, 401)

    def test_customer_session_cannot_read_admin_dashboard(self) -> None:
        shopper = self.new_client()
        self.register(shopper)
        self.assertEqual(shopper.get("/api/admin/dashboard").status_code, 401)
        self.assertEqual(shopper.get("/api/admin/orders").status_code, 401)

    def test_dashboard_statistics_come_from_database_records(self) -> None:
        from backend.app.services import order_services

        stats = order_services.admin_dashboard()
        for key in (
            "total_customers",
            "total_orders",
            "pending_orders",
            "processing_orders",
            "completed_orders",
            "cancelled_orders",
            "total_revenue",
            "status_counts",
            "recent_orders",
        ):
            self.assertIn(key, stats)
        self.assertEqual(
            stats["total_orders"],
            len(order_repository.list_orders()),
        )
        self.assertGreaterEqual(stats["total_revenue"], 0)


if __name__ == "__main__":
    unittest.main()
