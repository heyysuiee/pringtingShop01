"""Confirm Received Payment with integrated OCR verification.

These tests exercise the whole path: a customer uploads a receipt (OCR runs on submission), the admin order
view exposes the extracted data and comparison, and the existing Confirm Received Payment action records
money only after an explicit confirmation. Corrections to extracted values must be audited and must never
credit the same reference twice.
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-confirm-ocr-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import create_session_token, SESSION_COOKIE  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import audit_repository, payment_method_repository, reference_claim_repository  # noqa: E402
from backend.app.services import ocr_services  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
ADMIN_EMAIL = "admin@example.test"
CUSTOMER_NAME = "Confirm OCR Customer"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
METHOD_ACCOUNTS = {
    "GCash": "Confirm Shop GCash",
    "Maya": "Confirm Shop Maya",
}


def unique_ref(prefix: str = "REF") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class StaticReceiptProvider(ocr_services.OCRProvider):
    name = "confirm-test-ocr"

    def __init__(self, text: str) -> None:
        self.text = text

    def available(self) -> bool:
        return True

    def extract_text(self, contents: bytes, mime_type: str) -> str:
        return self.text


@contextmanager
def scanned_text(text: str):
    original = ocr_services._provider
    ocr_services.set_provider(StaticReceiptProvider(text))
    try:
        yield
    finally:
        ocr_services.set_provider(original)


class ConfirmPaymentOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls._context.__enter__()
        cls._original_methods = payment_method_repository.list_methods()
        methods = {item["provider"].casefold(): item for item in cls._original_methods}
        for provider, account in METHOD_ACCOUNTS.items():
            values = {
                "account_name": account,
                "account_number": "0917" + str(2000000 + len(provider)),
                "instructions": "Pay the amount due to the shop account.",
                "active": True,
                "display_order": len(provider),
                "verification_method": "manual",
                "gateway_config": {},
            }
            existing = methods.get(provider.casefold())
            if existing is None:
                payment_method_repository.insert({"provider": provider, **values})
            else:
                payment_method_repository.update(existing["id"], values)

    @classmethod
    def tearDownClass(cls) -> None:
        original_ids = {item["id"] for item in cls._original_methods}
        for method in payment_method_repository.list_methods():
            if method["id"] not in original_ids:
                payment_method_repository.delete(method["id"])
        keys = ("provider", "account_name", "account_number", "instructions", "active", "display_order",
                "verification_method", "gateway_config")
        for original in cls._original_methods:
            payment_method_repository.update(original["id"], {key: original[key] for key in keys})
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()
        self.admin = TestClient(app)
        self.admin.cookies.set(SESSION_COOKIE, create_session_token(ADMIN_EMAIL, "admin"))
        self.shopper = TestClient(app)
        customer = self.shopper.post("/api/customer/register", json={
            "name": CUSTOMER_NAME,
            "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567",
            "messenger": "",
            "password": PASSWORD,
        })
        self.assertEqual(customer.status_code, 201, customer.text)
        self.email = customer.json()["customer"]["email"]

    # ------------------------------------------------------------ helpers

    def admin_view(self, reference: str) -> dict:
        response = self.admin.get(f"/api/admin/orders/{reference}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def customer_view(self, reference: str) -> dict:
        response = self.shopper.get(f"/api/orders/{reference}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def admin_post(self, reference: str, action: str, body: dict | None = None, expect: int = 200) -> dict:
        payload = {**(body or {})}
        payload.setdefault("expected_version", self.admin_view(reference)["version"])
        response = self.admin.post(f"/api/admin/orders/{reference}/{action}", json=payload)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def customer_post(self, reference: str, action: str, body: dict | None = None, expect: int = 200) -> dict:
        response = self.shopper.post(f"/api/orders/{reference}/{action}", json={"email": self.email, **(body or {})})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def place_order(self, total: float = 1000) -> str:
        response = self.shopper.post("/api/orders", json={
            "customer": {"name": CUSTOMER_NAME, "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": "white-mug"},
            "customization": {
                "quantity": 2,
                "options": {"size": "11-oz", "print_area": "one-side", "inner_color": "white-classic", "packaging": "no-packaging"},
                "notes": "",
            },
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": [],
        }, headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 201, response.text)
        reference = response.json()["reference"]
        self.admin_post(reference, "status", {"status": "Under Review"})
        quoted = self.admin_post(reference, "quotation", {"total_amount": total, "payment_instructions": "Pay the shop account."})
        self.customer_post(reference, "quotation/accept", {"quotation_revision": quoted["quotation"]["revision"]})
        return reference

    def proof(self) -> dict:
        contents = PNG_SIGNATURE + uuid.uuid4().bytes + b"0" * 32
        return {"name": "receipt.png", "dataUrl": "data:image/png;base64," + base64.b64encode(contents).decode()}

    def receipt_text(self, payment_reference: str, amount: float, method: str = "GCash", recipient: str | None = None) -> str:
        return "\n".join([
            f"{method} payment receipt",
            f"Reference: {payment_reference}",
            f"Date: {date.today().isoformat()} 14:30",
            f"Sender: {CUSTOMER_NAME}",
            f"Recipient: {recipient or METHOD_ACCOUNTS[method]}",
            f"Payment method: {method}",
            f"Amount: PHP {amount:.2f}",
            "Status: Success",
        ])

    def submit_receipt(self, reference: str, payment_reference: str, amount: float, *, method: str = "GCash",
                       text: str | None = None) -> dict:
        body = text if text is not None else self.receipt_text(payment_reference, amount, method)
        with scanned_text(body):
            return self.customer_post(reference, "payments", {
                "amount": 500,
                "method": method,
                "reference": payment_reference,
                "proof": self.proof(),
            })

    def pending(self, reference: str) -> dict:
        return next(item for item in self.admin_view(reference)["payment_submissions"] if item["status"] == "pending")

    # ------------------------------------------------------------ tests

    def test_admin_modal_data_is_prefilled_from_ocr_and_never_credits_money_by_itself(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("GCASH")
        self.submit_receipt(reference, payment_reference, 500)

        view = self.admin_view(reference)
        submission = self.pending(reference)
        confirmation = submission["confirmation"]

        self.assertEqual(confirmation["expected_amount"], 500.0)
        self.assertEqual(confirmation["extracted_amount"], 500.0)
        self.assertEqual(confirmation["amount_difference"], 0.0)
        self.assertTrue(confirmation["amount_match"])
        self.assertEqual(confirmation["extracted_reference"], payment_reference)
        self.assertTrue(confirmation["reference_matched"])
        self.assertEqual(confirmation["down_payment_required"], 500.0)
        self.assertEqual(confirmation["previously_received"], 0.0)
        self.assertEqual(confirmation["remaining_balance"], 1000.0)
        labels = [status["label"] for status in confirmation["statuses"]]
        for expected in ("OCR Successfully Extracted", "Amount Matched", "Reference Matched", "Awaiting Admin Confirmation"):
            self.assertIn(expected, labels)
        self.assertEqual(submission["comparison"]["receipt_url"], submission["proof"]["url"])

        # Reading the receipt is not receiving money.
        self.assertEqual(view["payments"], [])
        self.assertEqual(view["summary"]["amount_paid"], 0.0)
        self.assertEqual(view["summary"]["pending_verification"], 500.0)

    def test_short_receipt_shows_insufficient_payment_and_partial_confirmation_keeps_down_payment_open(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("SHORT")
        self.submit_receipt(reference, payment_reference, 450)

        confirmation = self.pending(reference)["confirmation"]
        self.assertFalse(confirmation["amount_match"])
        self.assertEqual(confirmation["amount_shortfall"], 50.0)
        self.assertIn("Short by PHP 50.00", " ".join(item["label"] for item in confirmation["statuses"]))
        self.assertEqual(self.admin_view(reference)["payments"], [])

        self.admin_post(reference, f"payment-submissions/{self.pending(reference)['id']}/verify", {
            "received": True, "amount": 450,
        })
        view = self.admin_view(reference)
        self.assertEqual(len(view["payments"]), 1)
        self.assertEqual(view["payments"][0]["amount"], 450)
        self.assertEqual(view["summary"]["amount_paid"], 450.0)
        self.assertEqual(view["payment"]["down_payment_remaining"], 50.0)
        self.assertEqual(view["summary"]["balance"], 550.0)
        self.assertEqual(view["status"], "Awaiting Down Payment")
        self.assertEqual(self.customer_view(reference)["payment"]["amount_paid"], 450.0)

    def test_admin_corrections_are_saved_audited_and_cannot_credit_twice(self) -> None:
        reference = self.place_order()
        entered_reference = unique_ref("ENTERED")
        read_reference = unique_ref("MAYA")
        # The customer picked GCash and typed one reference; the receipt says Maya with a different reference.
        self.submit_receipt(
            reference, entered_reference, 500, method="GCash",
            text=self.receipt_text(read_reference, 500, "Maya"),
        )
        submission = self.pending(reference)
        confirmation = submission["confirmation"]
        self.assertFalse(confirmation["reference_matched"])
        self.assertEqual(confirmation["extracted_reference"], read_reference)

        result = self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {
            "received": True,
            "amount": 500,
            "method": "Maya",
            "reference": read_reference,
            "verification_note": "Checked in the shop's Maya account.",
            "correction_note": "OCR read a Maya receipt; the customer chose the wrong method and reference.",
        })
        payment = next(item for item in result["payments"] if item.get("submission_id") == submission["id"])
        self.assertEqual(payment["method"], "Maya")
        self.assertEqual(payment["reference"], read_reference)
        self.assertEqual(payment["verification"]["note"], "Checked in the shop's Maya account.")

        audit = audit_repository.list_for_order(reference)
        correction = next(item for item in audit if item["action"] == "payment.ocr_corrected")
        details = correction["details"]
        fields = {item["field"]: item for item in details["corrections"]}
        self.assertEqual(set(fields), {"method", "reference"})
        self.assertEqual(fields["reference"]["ocr_value"], read_reference)
        self.assertEqual(fields["reference"]["submitted_value"], entered_reference)
        self.assertEqual(fields["reference"]["final_value"], read_reference)
        self.assertIn("submission", fields["method"]["changed_from"])
        self.assertIn("OCR read a Maya receipt", details["reason"])
        self.assertTrue(any(item["action"] == "payment.verified" for item in audit))
        self.assertTrue(reference_claim_repository.is_used("payment", read_reference.replace("-", "").casefold()))

        self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {
            "received": True, "amount": 500,
        }, expect=409)
        self.assertEqual(len(self.admin_view(reference)["payments"]), 1)

    def test_correcting_to_a_reference_used_on_another_order_is_refused(self) -> None:
        first = self.place_order()
        first_reference = unique_ref("FIRST")
        self.submit_receipt(first, first_reference, 500)
        first_submission = self.pending(first)
        self.admin_post(first, f"payment-submissions/{first_submission['id']}/verify", {"received": True, "amount": 500})

        second = self.place_order()
        second_reference = unique_ref("SECOND")
        self.submit_receipt(second, second_reference, 500)
        second_submission = self.pending(second)
        result = self.admin_post(second, f"payment-submissions/{second_submission['id']}/verify", {
            "received": True, "amount": 500, "reference": first_reference,
        }, expect=409)
        self.assertIn("already been", result["detail"])
        self.assertEqual(self.admin_view(second)["payments"], [])
        self.assertIsNotNone(self.pending(second))

    def test_unreadable_receipt_is_marked_for_manual_review_without_recording_money(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("BLURRY")
        self.submit_receipt(reference, payment_reference, 500, text="blurry receipt with no readable details")

        confirmation = self.pending(reference)["confirmation"]
        labels = [item["label"] for item in confirmation["statuses"]]
        self.assertIn("Needs Manual Review", labels)
        self.assertNotIn("OCR Successfully Extracted", labels)
        self.assertNotIn("Amount Matched", labels)
        self.assertEqual(self.admin_view(reference)["payments"], [])

        # A receipt with nothing readable at all is shown as still awaiting processing.
        empty = self.place_order()
        self.submit_receipt(empty, unique_ref("EMPTY"), 500, text="")
        empty_labels = [item["label"] for item in self.pending(empty)["confirmation"]["statuses"]]
        self.assertIn("OCR Processing", empty_labels)
        self.assertEqual(self.admin_view(empty)["payments"], [])


if __name__ == "__main__":
    unittest.main()
