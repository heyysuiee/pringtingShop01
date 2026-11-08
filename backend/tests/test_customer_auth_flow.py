from __future__ import annotations

import json
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
# Never read the developer's .env file (SMTP credentials, production mode) during tests.
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from dataclasses import replace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.routes import auth as auth_routes  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database import connection as connection_module  # noqa: E402
from backend.app.repositories import auth_throttle_repository, customer_repository  # noqa: E402
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
        auth_throttle_repository.clear()
        self.sent: list[tuple[str, str]] = []

        def capture(recipient: str, subject: str, body: str, html_body: str | None = None) -> None:
            del subject, html_body  # Same signature as email_service.send_message; the tests only read the body.
            self.sent.append((recipient, body))

        patcher = patch.object(email_service, "send_message", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Pretend SMTP is configured; the route refuses reset requests when it is not.
        smtp_patcher = patch.object(
            auth_routes, "settings", replace(settings, smtp_host="smtp.example.test", smtp_from="shop@example.test")
        )
        smtp_patcher.start()
        self.addCleanup(smtp_patcher.stop)

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


class OrderUpdateTests(AuthFlowTestCase):
    def test_update_changes_allowed_fields_and_never_identity_fields(self) -> None:
        shopper = self.new_client()
        account = self.register(shopper)
        reference = shopper.post(
            "/api/orders",
            json=order_payload(account["email"]),
            headers={"Idempotency-Key": uuid.uuid4().hex + uuid.uuid4().hex[:8]},
        ).json()["reference"]
        before = order_repository.get_order(reference)
        # A stray key (only possible by bypassing the types) must not overwrite order identity or customer data.
        updates = {"status": "Under Review", "admin_notes": "Checked", "reference": "HACKED", "customer": {"email": "x@y.z"}}
        updated = order_repository.update_order(reference, updates, "Test update")  # type: ignore[arg-type]
        self.assertEqual(updated["status"], "Under Review")
        self.assertEqual(updated["admin_notes"], "Checked")
        self.assertEqual(updated["reference"], reference)
        self.assertEqual(updated["customer"], before["customer"])
        stored = order_repository.get_order(reference)
        self.assertEqual(stored["status"], "Under Review")
        self.assertEqual(stored["customer"]["email"], account["email"])
        self.assertEqual(len(stored["history"]), len(before["history"]) + 1)


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

    def test_failed_send_does_not_start_the_resend_cooldown(self) -> None:
        customer = self.register()
        with patch.object(email_service, "send_message", side_effect=email_service.EmailDeliveryError("down")):
            failed = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertEqual(failed.status_code, 503)
        # The retry must really send a code, not answer "use the code from the earlier email".
        retry = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertEqual(retry.status_code, 200)
        self.assertNotIn("cooldown", retry.json())
        self.assertEqual(len(self.sent), 1)
        self.assertRegex(self.last_code(), r"^\d{6}$")

    def test_missing_smtp_settings_give_the_same_503_for_every_address(self) -> None:
        customer = self.register()
        with patch.object(auth_routes, "settings", replace(settings, smtp_host="", smtp_from="")):
            known = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
            unknown = self.client.post("/api/customer/password-reset/request", json={"email": unique_email()})
        self.assertEqual(known.status_code, 503)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(self.sent, [])
        self.assertIsNone(customer_repository.get_reset(customer["id"]))

    def test_resubmitting_a_used_code_does_not_cancel_the_reset_session(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        code = self.last_code()
        token = self.client.post(
            "/api/customer/password-reset/verify", json={"email": customer["email"], "code": code}
        ).json()["reset_token"]
        repeat = self.client.post("/api/customer/password-reset/verify", json={"email": customer["email"], "code": code})
        self.assertEqual(repeat.status_code, 422)
        confirmed = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": token, "new_password": NEW_PASSWORD},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_short_new_password_is_rejected(self) -> None:
        customer = self.register()
        response = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": "x" * 30, "new_password": "short"},
        )
        self.assertEqual(response.status_code, 422)


class PasswordResetRulesTests(AuthFlowTestCase):
    def test_code_expires_after_five_minutes_and_response_says_so(self) -> None:
        customer = self.register()
        response = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertEqual(response.json()["expires_in"], 300)
        record = customer_repository.get_reset(customer["id"])
        lifetime = datetime.fromisoformat(record["code_expires_at"]) - datetime.fromisoformat(record["created_at"])
        self.assertLessEqual(lifetime, timedelta(minutes=5))
        self.assertGreater(lifetime, timedelta(minutes=4))

    def test_unknown_and_known_emails_get_identical_responses(self) -> None:
        customer = self.register()
        known = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]}).json()
        unknown = self.client.post("/api/customer/password-reset/request", json={"email": unique_email()}).json()
        self.assertEqual(known, unknown)
        self.assertNotIn("code", json.dumps(known).lower().replace("verification code has been sent", ""))

    def test_cooldown_response_does_not_claim_a_new_code(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        second = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]}).json()
        self.assertTrue(second["cooldown"])
        self.assertEqual(len(self.sent), 1)

    def test_new_password_must_differ_from_current(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        token = self.client.post(
            "/api/customer/password-reset/verify", json={"email": customer["email"], "code": self.last_code()}
        ).json()["reset_token"]
        same = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": token, "new_password": PASSWORD},
        )
        self.assertEqual(same.status_code, 422)
        self.assertIn("different", same.json()["detail"])
        # The session is still usable for a different password.
        changed = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": token, "new_password": NEW_PASSWORD},
        )
        self.assertEqual(changed.status_code, 200)

    def test_reset_token_cannot_change_another_customers_password(self) -> None:
        victim = self.register()
        attacker = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": attacker["email"]})
        token = self.client.post(
            "/api/customer/password-reset/verify", json={"email": attacker["email"], "code": self.last_code()}
        ).json()["reset_token"]
        hijack = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": victim["email"], "reset_token": token, "new_password": NEW_PASSWORD},
        )
        self.assertEqual(hijack.status_code, 422)
        self.assertIsNotNone(auth_services.authenticate_customer(victim["email"], PASSWORD))

    def test_sign_in_with_new_password_over_http_after_reset(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        token = self.client.post(
            "/api/customer/password-reset/verify", json={"email": customer["email"], "code": self.last_code()}
        ).json()["reset_token"]
        self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": customer["email"], "reset_token": token, "new_password": NEW_PASSWORD},
        )
        browser = self.new_client()
        old = browser.post("/api/customer/session", json={"email": customer["email"], "password": PASSWORD})
        self.assertEqual(old.status_code, 401)
        new = browser.post("/api/customer/session", json={"email": customer["email"], "password": NEW_PASSWORD})
        self.assertEqual(new.status_code, 200)
        self.assertEqual(browser.get("/api/customer/session").json()["customer"]["email"], customer["email"])


class SessionInvalidationTests(AuthFlowTestCase):
    def reset_password(self, email: str, new_password: str = NEW_PASSWORD) -> None:
        self.client.post("/api/customer/password-reset/request", json={"email": email})
        token = self.client.post(
            "/api/customer/password-reset/verify", json={"email": email, "code": self.last_code()}
        ).json()["reset_token"]
        confirmed = self.client.post(
            "/api/customer/password-reset/confirm",
            json={"email": email, "reset_token": token, "new_password": new_password},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_password_reset_signs_out_every_device(self) -> None:
        laptop = self.new_client()
        customer = self.register(laptop)
        phone = self.new_client()
        signed_in = phone.post(
            "/api/customer/session", json={"email": customer["email"], "password": PASSWORD, "remember": True}
        )
        self.assertEqual(signed_in.status_code, 200)
        for device in (laptop, phone):
            self.assertEqual(device.get("/api/customer/session").status_code, 200)

        self.reset_password(customer["email"])

        for device in (laptop, phone):
            self.assertEqual(device.get("/api/customer/session").status_code, 401)
            self.assertEqual(device.get("/api/account/profile").status_code, 401)
            self.assertEqual(device.get("/api/account/orders").status_code, 401)
        # Signing in again with the new password gives a working session.
        again = self.new_client()
        self.assertEqual(
            again.post("/api/customer/session", json={"email": customer["email"], "password": NEW_PASSWORD}).status_code, 200
        )
        self.assertEqual(again.get("/api/customer/session").status_code, 200)

    def test_reset_of_one_account_leaves_other_accounts_signed_in(self) -> None:
        other_device = self.new_client()
        self.register(other_device)
        target = self.register(self.new_client())
        self.reset_password(target["email"])
        self.assertEqual(other_device.get("/api/customer/session").status_code, 200)

    def test_cookie_issued_before_session_versions_existed_still_works_until_a_reset(self) -> None:
        customer = self.register(self.new_client())
        legacy = self.new_client()
        legacy.cookies.set(SESSION_COOKIE, create_session_token(customer["id"], "customer"))  # no "ver" claim
        self.assertEqual(legacy.get("/api/customer/session").status_code, 200)
        self.reset_password(customer["email"])
        self.assertEqual(legacy.get("/api/customer/session").status_code, 401)

    def test_existing_accounts_are_kept_when_the_column_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            legacy_db = Path(folder) / "legacy.sqlite3"
            with patch.object(connection_module, "DATABASE_PATH", legacy_db):
                with connection_scope() as connection:
                    connection.execute(
                        """CREATE TABLE customer_accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        name TEXT NOT NULL, phone TEXT NOT NULL, messenger TEXT NOT NULL DEFAULT '',
                        password_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
                    )
                    connection.execute(
                        "INSERT INTO customer_accounts VALUES ('old-1', 'old@example.test', 'Old', '1234567', '', 'hash', 't', 't')"
                    )
                customer_repository.initialize()
                customer_repository.initialize()  # running it twice is harmless
                self.assertEqual(customer_repository.get_session_version("old-1"), 0)
                self.assertEqual(customer_repository.get_by_id("old-1")["email"], "old@example.test")


class PersistentOtpLimitTests(AuthFlowTestCase):
    def test_resend_cooldown_survives_a_restart(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        limiter.clear()  # what a restart does to the in-memory limiter
        again = self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        self.assertTrue(again.json().get("cooldown"))
        self.assertEqual(len(self.sent), 1)

    def test_hourly_request_limit_survives_a_restart(self) -> None:
        email = unique_email()
        for _ in range(5):
            auth_throttle_repository.reset("otp-cooldown", email)  # skip the 60-second wait between requests
            self.assertEqual(self.client.post("/api/customer/password-reset/request", json={"email": email}).status_code, 200)
        limiter.clear()
        auth_throttle_repository.reset("otp-cooldown", email)
        sixth = self.client.post("/api/customer/password-reset/request", json={"email": email})
        self.assertEqual(sixth.status_code, 429)

    def test_wrong_attempt_count_survives_a_restart(self) -> None:
        customer = self.register()
        self.client.post("/api/customer/password-reset/request", json={"email": customer["email"]})
        real_code = self.last_code()
        wrong = "000000" if real_code != "000000" else "111111"
        for _ in range(4):
            self.client.post("/api/customer/password-reset/verify", json={"email": customer["email"], "code": wrong})
        limiter.clear()
        fifth = self.client.post("/api/customer/password-reset/verify", json={"email": customer["email"], "code": wrong})
        self.assertEqual(fifth.status_code, 429)

    def test_stored_keys_are_hashed_not_plain_email_addresses(self) -> None:
        email = unique_email()
        self.client.post("/api/customer/password-reset/request", json={"email": email})
        with connection_scope() as connection:
            rows = connection.execute("SELECT bucket, key_hash FROM auth_throttle_events").fetchall()
        self.assertTrue(rows)
        self.assertFalse(any(email in row["key_hash"] for row in rows))
        self.assertEqual({len(row["key_hash"]) for row in rows}, {64})


class RememberMeTests(AuthFlowTestCase):
    def test_remember_me_sets_a_30_day_cookie_and_default_is_browser_session(self) -> None:
        customer = self.register()
        remembered = self.new_client().post(
            "/api/customer/session", json={"email": customer["email"], "password": PASSWORD, "remember": True}
        )
        self.assertIn(f"Max-Age={30 * 24 * 60 * 60}", remembered.headers["set-cookie"])
        session_only = self.new_client().post(
            "/api/customer/session", json={"email": customer["email"], "password": PASSWORD}
        )
        self.assertNotIn("Max-Age", session_only.headers["set-cookie"])
        self.assertIn("HttpOnly", session_only.headers["set-cookie"])


class AuthPageTests(AuthFlowTestCase):
    def test_auth_pages_are_served(self) -> None:
        for path, marker in (
            ("/sign-in", 'data-auth-page="sign-in"'),
            ("/create-account", 'data-auth-page="create-account"'),
            ("/forgot-password", 'data-auth-page="forgot-password"'),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.text)
        self.assertEqual(self.client.get("/register", follow_redirects=False).headers["location"], "/create-account")

    def test_sign_in_page_does_not_contain_the_registration_form(self) -> None:
        html = self.client.get("/sign-in").text
        self.assertNotIn("create-account-form", html)
        self.assertIn('href="/create-account"', html)
        self.assertIn("Forgot password?", html)


class CacheHeaderTests(AuthFlowTestCase):
    def test_pages_scripts_and_styles_are_revalidated_on_every_load(self) -> None:
        for path in ("/customer-portal", "/assets/portal.js", "/assets/account.js", "/assets/style.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers.get("cache-control"), "no-cache", path)
        # Unchanged files are answered with 304, so revalidating stays cheap.
        first = self.client.get("/assets/portal.js")
        again = self.client.get("/assets/portal.js", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)

    def test_images_and_api_responses_are_not_changed(self) -> None:
        self.assertNotIn("cache-control", self.client.get("/assets/image/image.jpg").headers)
        self.assertNotIn("cache-control", self.client.get("/api/health").headers)
        # A header an endpoint sets itself is kept.
        self.assertEqual(self.client.get("/api/catalog").headers.get("cache-control"), "no-store")


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
