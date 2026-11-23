"""Validation and location checks added during the UI/UX improvement pass."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


_test_directory = tempfile.TemporaryDirectory(prefix="impress-ui-ux-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend.app import main  # noqa: E402
from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.schemas.order import (  # noqa: E402
    AdminConfirmation,
    AdminOrderStatus,
    AdminPaymentReference,
    AdminQuotation,
    CustomerOrderChanges,
)
from backend.app.schemas.user import validate_phone  # noqa: E402
from backend.app.services import location_services  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
MAGNET_OPTIONS = {
    "size": "2-x-3-in",
    "shape": "rectangle",
    "material": "flexible-rubber-magnet",
    "finish": "glossy-lamination",
    "packaging": "no-packaging",
}


class UiUxValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(main.app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()

    def new_customer(self) -> tuple[TestClient, dict]:
        shopper = TestClient(main.app)
        response = shopper.post("/api/customer/register", json={
            "name": "Validation Customer",
            "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567",
            "messenger": "",
            "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return shopper, response.json()["customer"]

    def admin_client(self) -> TestClient:
        admin = TestClient(main.app)
        admin.cookies.set(SESSION_COOKIE, create_session_token("admin@example.test", "admin"))
        return admin

    def place_order(self, shopper: TestClient, email: str, fulfillment: dict | None = None) -> dict:
        payload = {
            "customer": {"name": "Validation Customer", "email": email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": "refrigerator-magnet"},
            "customization": {"quantity": 20, "options": MAGNET_OPTIONS, "notes": ""},
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": [],
            "field_files": {},
        }
        if fulfillment is not None:
            payload["fulfillment"] = fulfillment
        response = shopper.post("/api/orders", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex * 2})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_locations_endpoint_exposes_dependent_address_data(self) -> None:
        response = self.client.get("/api/locations")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["source"])
        self.assertTrue(body["regions"])
        self.assertGreaterEqual(len(body["provinces"]), 80)
        metro = next(item for item in body["provinces"] if item["name"] == "Metro Manila (NCR)")
        self.assertIn("City of Manila", metro["cities"])
        self.assertTrue(all(isinstance(city, str) for province in body["provinces"] for city in province["cities"]))

    def test_address_combination_is_canonicalized_and_checked(self) -> None:
        address = location_services.resolve_address("Cavite", "Cavite City")
        self.assertEqual(address["province"], "Cavite")
        self.assertEqual(address["city"], "City of Cavite")
        with self.assertRaises(HTTPException) as error:
            location_services.resolve_address("Cavite", "Manila")
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("City / Municipality", error.exception.detail)

    def test_mismatched_address_is_rejected_before_the_order_is_saved(self) -> None:
        shopper, customer = self.new_customer()
        fulfillment = {
            "method": "delivery",
            "recipient_name": "Validation Customer",
            "contact_number": "0917 555 1234",
            "street": "1 Sample Street",
            "barangay": "Sample Barangay",
            "city": "Manila",
            "province": "Cavite",
            "landmark": "",
        }
        response = shopper.post("/api/orders", json={
            "customer": {"name": customer["name"], "email": customer["email"], "phone": customer["phone"], "messenger": ""},
            "product": {"id": "refrigerator-magnet"},
            "customization": {"quantity": 20, "options": MAGNET_OPTIONS, "notes": ""},
            "event": {}, "has_event": False, "acknowledged": True,
            "files": [], "field_files": {}, "fulfillment": fulfillment,
        }, headers={"Idempotency-Key": uuid.uuid4().hex * 2})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("City / Municipality", response.json()["detail"])

    def test_phone_validation_accepts_ph_formats_and_rejects_short_numbers(self) -> None:
        for value in ("0917 555 1234", "+63 917 123 4567", "09171234567", "02 1234 5678"):
            self.assertEqual(validate_phone(value), value)
        for value in ("", "abc", "1234567", "0917"):
            with self.assertRaises(ValueError):
                validate_phone(value)

    def test_payment_method_metadata_matches_backend_upload_limits(self) -> None:
        body = self.client.get("/api/payment-methods").json()
        self.assertGreater(body["max_proof_bytes"], 0)
        self.assertIn("image/png", body["allowed_proof_types"])
        self.assertIn("application/pdf", body["allowed_proof_types"])
        self.assertTrue(body["methods"])

    def test_rejecting_or_cancelling_requires_a_reason(self) -> None:
        shopper, customer = self.new_customer()
        order = self.place_order(shopper, customer["email"])
        admin = self.admin_client()
        url = f"/api/admin/orders/{order['reference']}/status"
        version = admin.get(f"/api/admin/orders/{order['reference']}").json()["version"]
        missing = admin.post(url, json={"status": "Rejected", "expected_version": version})
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertIn("reason", missing.json()["detail"].lower())
        accepted = admin.post(url, json={
            "status": "Rejected", "note": "Artwork does not meet the print specification.", "expected_version": version,
        })
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "Rejected")

    def test_action_schemas_bound_text_fields(self) -> None:
        with self.assertRaises(ValidationError) as error:
            AdminOrderStatus(status="Rejected", note="x" * 501, expected_version=1)
        self.assertIn("Reason", str(error.exception))

        # Schema caps must match the service caps they wrap, or valid text fails before the service runs.
        self.assertEqual(len(CustomerOrderChanges(note="x" * 1000).note), 1000)
        with self.assertRaises(ValidationError):
            CustomerOrderChanges(note="x" * 1001)

        quotation = AdminQuotation(total_amount=1000, payment_instructions="i" * 1000, note="n" * 1000, expected_version=1)
        self.assertEqual(len(quotation.payment_instructions), 1000)
        self.assertEqual(len(quotation.note), 1000)
        with self.assertRaises(ValidationError):
            AdminQuotation(payment_instructions="i" * 1001, expected_version=1)
        with self.assertRaises(ValidationError):
            AdminQuotation(note="n" * 1001, expected_version=1)

        self.assertEqual(len(AdminConfirmation(payment_instructions="i" * 1000, expected_version=1).payment_instructions), 1000)
        with self.assertRaises(ValidationError):
            AdminConfirmation(payment_instructions="i" * 1001, expected_version=1)

        self.assertEqual(len(AdminPaymentReference(note="n" * 300, expected_version=1).note), 300)
        with self.assertRaises(ValidationError):
            AdminPaymentReference(note="n" * 301, expected_version=1)

    def test_oversized_inline_upload_body_is_rejected_before_parsing(self) -> None:
        shopper, customer = self.new_customer()
        order = self.place_order(shopper, customer["email"])
        # Use a tiny limit so the test does not allocate a multi-megabyte payload.
        with mock.patch.object(main, "INLINE_BODY_LIMIT", 32):
            response = shopper.post(
                f"/api/orders/{order['reference']}/payments",
                json={"email": customer["email"], "reference": "1234567890123456789012345678901234567890"},
            )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertIn("too large", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
