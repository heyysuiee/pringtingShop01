"""OCR-assisted payment receipt verification.

These tests stub the OCR engine and the optional provider API so they are deterministic on machines without
Tesseract. They cover the required receipt scenarios and, above all, the money rule: reading a receipt never
records a payment. Only an explicit administrator verification or a positive independent provider confirmation
may do that.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_test_directory = tempfile.TemporaryDirectory(prefix="impress-ocr-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import create_session_token, SESSION_COOKIE  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import payment_method_repository  # noqa: E402
from backend.app.services import ocr_services  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
ADMIN_EMAIL = "admin@example.test"
CUSTOMER_NAME = "OCR Customer"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

METHOD_ACCOUNTS = {
    "GCash": "Print Works GCash",
    "Maya": "Print Works Maya",
    "MariBank": "Print Works MariBank",
    "Bank Transfer": "Print Works Bank Account",
}


def unique_ref(prefix: str = "OCR") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class StaticReceiptProvider(ocr_services.OCRProvider):
    """A deterministic OCR engine used by the tests (the real one is lazy and optional)."""

    name = "test-ocr"

    def __init__(self, text: str, error: Exception | None = None) -> None:
        self.text = text
        self.error = error

    def available(self) -> bool:
        return True

    def extract_text(self, contents: bytes, mime_type: str) -> str:
        if self.error is not None:
            raise self.error
        return self.text


@contextmanager
def scanned_text(text: str | None = None, error: Exception | None = None):
    """Replace the OCR provider for one receipt scan and always restore the configured provider."""
    original = ocr_services._provider
    ocr_services.set_provider(StaticReceiptProvider(text or "", error))
    try:
        yield
    finally:
        ocr_services.set_provider(original)


class OCRPaymentVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()
        cls._original_methods = payment_method_repository.list_methods()
        cls._configure_methods()

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

    @classmethod
    def _configure_methods(cls) -> None:
        methods = {item["provider"].casefold(): item for item in payment_method_repository.list_methods()}
        for provider, account in METHOD_ACCOUNTS.items():
            existing = methods.get(provider.casefold())
            values = {
                "account_name": account,
                "account_number": "0917" + str(1000000 + len(provider)),
                "instructions": f"Pay exactly the amount due to {account}.",
                "active": True,
                "display_order": len(provider),
                "verification_method": "manual",
                "gateway_config": {},
            }
            if existing is None:
                payment_method_repository.insert({"provider": provider, **values})
            else:
                payment_method_repository.update(existing["id"], values)

    def setUp(self) -> None:
        limiter.clear()
        self.admin = TestClient(app)
        self.admin.cookies.set(SESSION_COOKIE, create_session_token(ADMIN_EMAIL, "admin"))
        self.shopper, self.email = self._new_customer()

    # ------------------------------------------------------------ helpers

    def _new_customer(self) -> tuple[TestClient, str]:
        shopper = TestClient(app)
        response = shopper.post("/api/customer/register", json={
            "name": CUSTOMER_NAME,
            "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567",
            "messenger": "",
            "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return shopper, response.json()["customer"]["email"]

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
        response = self.shopper.post(
            f"/api/orders/{reference}/{action}",
            json={"email": self.email, **(body or {})},
        )
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
        quoted = self.admin_post(reference, "quotation", {
            "total_amount": total,
            "payment_instructions": "Pay to the shop's e-wallet account.",
        })
        self.customer_post(reference, "quotation/accept", {"quotation_revision": quoted["quotation"]["revision"]})
        return reference

    def proof(self, contents: bytes | None = None) -> dict:
        # A unique file makes its hash unique; duplicate tests deliberately reuse one payload.
        payload = contents or PNG_SIGNATURE + uuid.uuid4().bytes + b"0" * 32
        return {"name": "receipt.png", "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode()}

    def receipt_text(
        self,
        reference: str,
        amount: float,
        method: str,
        *,
        recipient: str | None = None,
        sender: str = CUSTOMER_NAME,
        status: str = "Success",
        transaction_date: str | None = None,
        extra: str = "",
    ) -> str:
        return "\n".join([
            f"{method} payment receipt",
            f"Reference: {reference}",
            f"Date: {transaction_date or date.today().isoformat()} 14:30",
            f"Sender: {sender}",
            f"Recipient: {recipient if recipient is not None else METHOD_ACCOUNTS[method]}",
            f"Payment method: {method}",
            f"Amount: PHP {amount:.2f}",
            f"Status: {status}",
            extra,
        ])

    def scan(self, reference: str, method: str, payment_reference: str, text: str, *, proof: dict | None = None) -> dict:
        with scanned_text(text):
            return self.customer_post(reference, "payments/scan", {
                "method": method,
                "reference": payment_reference,
                "proof": proof or self.proof(),
            })

    def submit(
        self,
        reference: str,
        method: str,
        payment_reference: str,
        text: str,
        *,
        amount: float = 500,
        proof: dict | None = None,
        expect: int = 200,
    ) -> dict:
        with scanned_text(text):
            return self.customer_post(reference, "payments", {
                "amount": amount,
                "method": method,
                "reference": payment_reference,
                "proof": proof or self.proof(),
            }, expect)

    def pending_submission(self, reference: str) -> dict:
        return next(item for item in self.admin_view(reference)["payment_submissions"] if item["status"] == "pending")

    def verify_submission(self, reference: str, amount: float = 500) -> dict:
        submission = self.pending_submission(reference)
        return self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {
            "received": True,
            "amount": amount,
        })

    def enable_provider_api(self, provider: str, endpoint: str = "https://provider.example.test/verify") -> None:
        methods = self.admin.get("/api/admin/settings/payment-methods").json()["methods"]
        method = next(item for item in methods if item["provider"] == provider)
        response = self.admin.patch(f"/api/admin/settings/payment-methods/{method['id']}", json={
            "verification_method": "api",
            "gateway_config": {
                "provider": "Test Payment Gateway",
                "endpoint": endpoint,
                "api_key": "super-secret-test-key",
                "timeout_seconds": 5,
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        # The key is stored for the server only; no view leaks it to the browser.
        self.assertNotIn("super-secret-test-key", json.dumps(response.json()))
        public = TestClient(app).get("/api/payment-methods").json()
        self.assertNotIn("super-secret-test-key", json.dumps(public))

    def reach_balance_stage(self, reference: str) -> None:
        down_reference = unique_ref("DOWN")
        self.submit(reference, "GCash", down_reference, self.receipt_text(down_reference, 500, "GCash"))
        self.verify_submission(reference, 500)
        self.assertEqual(self.admin_view(reference)["status"], "Confirmed")
        self.admin_post(reference, "draft", {"file": self.proof()})
        self.customer_post(reference, "approve")
        self.admin_post(reference, "status", {"status": "In Production"})
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        self.admin_post(reference, "quality-check", {"checks": {
            "product_quality": True,
            "print_quality": True,
            "color_accuracy": True,
            "quantity_verified": True,
            "material_condition": True,
        }})
        packed = self.admin_post(reference, "packaging", {"completed_quantity": 2})
        self.assertEqual(packed["status"], "Awaiting Full Payment")

    # ------------------------------------------------------------ valid receipts

    def test_valid_gcash_receipt_is_read_and_stays_pending_without_payment(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("GCASH")
        result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))

        self.assertEqual(result["required_amount"], 500.0)
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertEqual(result["ocr"]["fields"]["reference"], payment_reference)
        self.assertEqual(result["ocr"]["fields"]["amount"], "500.00")
        self.assertEqual(result["ocr"]["fields"]["method"], "GCash")
        self.assertTrue(result["verification"]["auto_confirm"] is False)
        self.assertEqual(self.admin_view(reference)["payments"], [])

    def test_valid_maya_receipt_is_matched_to_the_selected_method(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("MAYA")
        result = self.scan(reference, "Maya", payment_reference, self.receipt_text(payment_reference, 500, "Maya"))

        self.assertEqual(result["method"], "Maya")
        self.assertEqual(result["ocr"]["fields"]["method"], "Maya")
        self.assertEqual(result["verification"]["status"], "pending")
        method_check = next(item for item in result["verification"]["checks"] if item["key"] == "method")
        self.assertEqual(method_check["state"], "match")

    def test_bank_transfer_receipt_is_recognized(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("BANK")
        result = self.scan(reference, "Bank Transfer", payment_reference, self.receipt_text(payment_reference, 500, "Bank Transfer"))

        self.assertEqual(result["method"], "Bank Transfer")
        self.assertEqual(result["ocr"]["fields"]["method"], "Bank Transfer")
        self.assertEqual(result["verification"]["status"], "pending")

    def test_matching_down_payment_stays_unpaid_until_admin_confirms_money(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("DOWN")
        text = self.receipt_text(payment_reference, 500, "GCash")
        self.submit(reference, "GCash", payment_reference, text)

        view = self.admin_view(reference)
        submission = self.pending_submission(reference)
        self.assertEqual(submission["verification_status"], "pending")
        self.assertEqual(submission["ocr"]["fields"]["amount"], "500.00")
        self.assertEqual(view["status"], "Awaiting Down Payment")
        self.assertEqual(view["payments"], [])

        verified = self.verify_submission(reference, 500)
        self.assertEqual(verified["status"], "Confirmed")
        self.assertEqual(len(verified["payments"]), 1)
        self.assertEqual(verified["payments"][0]["ocr"]["fields"]["reference"], payment_reference)

    # ------------------------------------------------------------ suspicious receipts

    def test_mismatched_amount_goes_to_admin_review_and_records_nothing(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("AMOUNT")
        result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 250, "GCash"))

        self.assertEqual(result["verification"]["status"], "admin_review")
        amount_check = next(item for item in result["verification"]["checks"] if item["key"] == "amount")
        self.assertEqual(amount_check["state"], "inconsistent")

        self.submit(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 250, "GCash"))
        self.assertEqual(self.admin_view(reference)["payments"], [])

    def test_duplicate_reference_is_classified_and_refused_on_submission(self) -> None:
        first = self.place_order()
        first_reference = unique_ref("DUP")
        self.submit(first, "GCash", first_reference, self.receipt_text(first_reference, 500, "GCash"))

        second = self.place_order()
        result = self.scan(second, "GCash", first_reference, self.receipt_text(first_reference, 500, "GCash"))
        self.assertEqual(result["verification"]["status"], "possible_duplicate")
        duplicate_check = next(item for item in result["verification"]["checks"] if item["key"] == "duplicate")
        self.assertEqual(duplicate_check["state"], "inconsistent")

        self.submit(second, "GCash", first_reference, self.receipt_text(first_reference, 500, "GCash"), expect=409)
        self.assertEqual(self.admin_view(second)["payments"], [])

    def test_same_receipt_file_is_flagged_even_with_a_new_reference(self) -> None:
        first = self.place_order()
        shared_file = self.proof()
        self.submit(first, "GCash", unique_ref("FILE1"), self.receipt_text(unique_ref("FILE1"), 500, "GCash"), proof=shared_file)

        second = self.place_order()
        new_reference = unique_ref("FILE2")
        result = self.scan(second, "GCash", new_reference, self.receipt_text(new_reference, 500, "GCash"), proof=shared_file)
        self.assertEqual(result["verification"]["status"], "possible_duplicate")

    def test_blurry_or_unreadable_receipt_is_sent_to_admin_review(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("BLUR")
        result = self.scan(reference, "GCash", payment_reference, "")

        self.assertEqual(result["ocr"]["fields"]["reference"], "")
        self.assertIn("unreadable", result["ocr"]["flags"])
        self.assertEqual(result["verification"]["status"], "admin_review")

        self.submit(reference, "GCash", payment_reference, "")
        self.assertEqual(self.pending_submission(reference)["verification_status"], "admin_review")
        self.assertEqual(self.admin_view(reference)["payments"], [])

    def test_wrong_recipient_is_inconsistent(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("WHO")
        result = self.scan(reference, "GCash", payment_reference,
                           self.receipt_text(payment_reference, 500, "GCash", recipient="Different Merchant"))

        self.assertEqual(result["verification"]["status"], "admin_review")
        recipient_check = next(item for item in result["verification"]["checks"] if item["key"] == "recipient")
        self.assertEqual(recipient_check["state"], "inconsistent")
        self.assertEqual(recipient_check["expected"], METHOD_ACCOUNTS["GCash"])

    def test_wrong_receipt_for_another_payment_is_not_accepted_as_proof(self) -> None:
        reference = self.place_order()
        entered_reference = unique_ref("ENTERED")
        other_reference = unique_ref("OTHER")
        text = self.receipt_text(other_reference, 999, "Maya", recipient="Someone Else")
        result = self.scan(reference, "GCash", entered_reference, text)

        self.assertEqual(result["verification"]["status"], "admin_review")
        states = {item["key"]: item["state"] for item in result["verification"]["checks"]}
        self.assertEqual(states["reference"], "inconsistent")
        self.assertEqual(states["amount"], "inconsistent")
        self.assertEqual(states["method"], "inconsistent")
        self.assertEqual(self.admin_view(reference)["payments"], [])

    def test_suspicious_receipt_reporting_failure_is_rejected(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("FAIL")
        text = self.receipt_text(payment_reference, 500, "GCash", status="Failed", extra="Status: Success")
        result = self.scan(reference, "GCash", payment_reference, text)

        self.assertEqual(result["verification"]["status"], "rejected")
        self.assertIn("failed_status", result["ocr"]["flags"])
        status_check = next(item for item in result["verification"]["checks"] if item["key"] == "status")
        self.assertEqual(status_check["state"], "inconsistent")

    def test_failed_ocr_engine_does_not_crash_the_payment_flow(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("CRASH")
        text = self.receipt_text(payment_reference, 500, "GCash")
        with scanned_text(error=RuntimeError("OCR engine unavailable")):
            result = self.customer_post(reference, "payments/scan", {
                "method": "GCash",
                "reference": payment_reference,
                "proof": self.proof(),
            })
        self.assertEqual(result["verification"]["status"], "admin_review")
        self.assertIn("ocr_error", result["ocr"]["flags"])

        with scanned_text(text):  # submission uses its own authoritative scan
            self.customer_post(reference, "payments", {
                "method": "GCash",
                "reference": payment_reference,
                "proof": self.proof(),
            })
        self.assertEqual(self.admin_view(reference)["payments"], [])

    # ------------------------------------------------------------ balances and provider confirmation

    def test_remaining_balance_receipt_uses_the_server_calculated_amount(self) -> None:
        reference = self.place_order()
        self.reach_balance_stage(reference)
        payment_reference = unique_ref("BAL")
        result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))

        self.assertEqual(result["label"], "Remaining balance")
        self.assertEqual(result["required_amount"], 500.0)
        self.assertEqual(result["verification"]["status"], "pending")

        self.submit(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        verified = self.verify_submission(reference, 500)
        self.assertEqual(verified["payments"][-1]["kind"], "Balance payment")
        self.assertEqual(verified["payment"]["balance"], 0.0)

    def test_full_payment_cannot_be_shortcut_before_the_down_payment(self) -> None:
        reference = self.place_order(1200)
        full_amount_ref = unique_ref("FULL")
        declared = self.submit(reference, "GCash", full_amount_ref, self.receipt_text(full_amount_ref, 1200, "GCash"),
                               amount=1200)
        submission = next(item for item in declared["payment_submissions"] if item["reference"] == full_amount_ref)
        self.assertEqual(submission["amount"], 1200.0)
        self.assertEqual(submission["status"], "pending")
        # The browser's declared amount is a claim, not money: nothing is credited until the shop verifies it.
        self.assertEqual(declared["payments"], [])
        self.assertEqual(declared["payment"]["amount_paid"], 0.0)
        self.assertEqual(declared["payment"]["payment_status"], "No Payment")
        self.admin_post(reference, f"payment-submissions/{submission['id']}/reject", {
            "note": "Customer declared the full amount before the down payment was verified",
        })

        # The correct 50% down payment is accepted; the remaining 50% is still outstanding.
        down_ref = unique_ref("DOWN")
        self.submit(reference, "GCash", down_ref, self.receipt_text(down_ref, 600, "GCash"), amount=600)
        self.verify_submission(reference, 600)
        confirmed = self.admin_view(reference)
        self.assertEqual(confirmed["status"], "Confirmed")
        self.assertEqual(confirmed["payment"]["balance"], 600.0)

    def test_unknown_payment_method_is_refused(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("UNKNOWN")
        payload = {"method": "Unknown Wallet", "reference": payment_reference, "proof": self.proof()}
        self.customer_post(reference, "payments/scan", payload, expect=422)
        self.customer_post(reference, "payments", {**payload, "amount": 500}, expect=422)
        self.assertEqual(self.admin_view(reference)["payment_submissions"], [])

    def test_provider_unavailable_never_verifies_a_receipt(self) -> None:
        reference = self.place_order()
        self.enable_provider_api("GCash")
        payment_reference = unique_ref("OFFLINE")

        def offline(*args, **kwargs):
            raise RuntimeError("provider offline")

        with patch("httpx.post", side_effect=offline):
            result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        provider = result["verification"]["provider"]
        self.assertTrue(provider["attempted"])
        self.assertFalse(provider["confirmed"])
        self.assertIn("unavailable", provider["detail"])
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertFalse(result["verification"]["auto_confirm"])

        with patch("httpx.post", side_effect=offline):
            self.submit(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertEqual(self.admin_view(reference)["payments"], [])

    def test_positive_independent_provider_confirmation_can_record_once(self) -> None:
        reference = self.place_order()
        self.enable_provider_api("GCash")
        payment_reference = unique_ref("API")

        def confirmed_provider(endpoint, *, json, headers, timeout):
            return SimpleNamespace(status_code=200, json=lambda: {
                "confirmed": True,
                "reference": payment_reference,
                "amount": 500.0,
                "status": "success",
            })

        with patch("httpx.post", side_effect=confirmed_provider):
            result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertTrue(result["verification"]["auto_confirm"])

        with patch("httpx.post", side_effect=confirmed_provider):
            recorded = self.submit(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertEqual(recorded["status"], "Confirmed")
        self.assertEqual(len(recorded["payments"]), 1)
        # The customer response must have the same shape as any other submission, not the admin view
        # returned by the verification service.
        self.assertNotIn("verification", recorded["payments"][0])
        self.assertNotIn("recorded_by", recorded["payments"][0])
        customer_submission = recorded["payment_submissions"][0]
        self.assertEqual(customer_submission["admin_notes"], [])
        self.assertEqual(customer_submission["receipt_requests"], [])
        self.assertNotIn("comparison", customer_submission)
        self.assertTrue(all("/api/orders/" in (item.get("proof") or {}).get("url", "") for item in recorded["payment_submissions"]))
        admin_recorded = self.admin_view(reference)
        self.assertEqual(admin_recorded["payments"][0]["verification"]["verified_by"], "Test Payment Gateway")
        self.assertNotIn("super-secret-test-key", json.dumps(recorded))
        self.assertNotIn("super-secret-test-key", json.dumps(admin_recorded))

    def test_provider_confirmation_for_a_different_amount_is_not_accepted(self) -> None:
        reference = self.place_order()
        self.enable_provider_api("GCash")
        payment_reference = unique_ref("MISMATCH")

        def wrong_amount(endpoint, *, json, headers, timeout):
            return SimpleNamespace(status_code=200, json=lambda: {
                "confirmed": True,
                "reference": payment_reference,
                "amount": 50.0,
            })

        with patch("httpx.post", side_effect=wrong_amount):
            result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertFalse(result["verification"]["provider"]["confirmed"])
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertFalse(result["verification"]["auto_confirm"])

    def test_only_pdf_text_is_read_and_a_pdf_without_text_goes_to_review(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("PDF")
        with scanned_text(""):
            result = self.customer_post(reference, "payments/scan", {
                "method": "GCash",
                "reference": payment_reference,
                "proof": {"name": "receipt.pdf", "dataUrl": "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4\n1 0 obj\n%%EOF").decode()},
            })
        self.assertEqual(result["verification"]["status"], "admin_review")
        self.assertIn("No text layer", " ".join(result["ocr"]["notes"]))

    # ------------------------------------------------------------ review follow-up

    def test_clearer_receipt_can_reuse_reference_with_a_new_file(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("CLEAR")
        self.submit(reference, "GCash", payment_reference, "", proof=self.proof())
        submission = self.pending_submission(reference)
        self.admin_post(reference, f"payment-submissions/{submission['id']}/request-receipt", {
            "note": "Please show the full receipt.",
        })

        customer = self.customer_view(reference)
        old = next(item for item in customer["payment_submissions"] if item["id"] == submission["id"])
        self.assertEqual(old["status"], "receipt_requested")
        self.assertEqual(old["receipt_requests"][0]["note"], "Please show the full receipt.")
        self.assertNotIn("by", old["receipt_requests"][0])

        text = self.receipt_text(payment_reference, 500, "GCash")
        with scanned_text(text):
            replacement = self.customer_post(reference, "payments", {
                "amount": 500,
                "method": "GCash",
                "reference": payment_reference,
                "proof": self.proof(),
            })
        self.assertEqual(replacement["status"], "Awaiting Down Payment")
        new_submission = self.pending_submission(reference)
        self.assertNotEqual(new_submission["id"], submission["id"])
        self.assertEqual(new_submission["verification_status"], "pending")
        self.assertEqual(len(self.admin_view(reference)["payment_submissions"]), 2)

        verified = self.verify_submission(reference, 500)
        self.assertEqual(verified["status"], "Confirmed")
        self.assertEqual(len(verified["payments"]), 1)

    def test_receipt_request_does_not_release_reference_for_another_order(self) -> None:
        first = self.place_order()
        payment_reference = unique_ref("SHARED")
        text = self.receipt_text(payment_reference, 500, "GCash")
        self.submit(first, "GCash", payment_reference, text)
        submission = self.pending_submission(first)
        self.admin_post(first, f"payment-submissions/{submission['id']}/request-receipt", {})

        second = self.place_order()
        payload = {"method": "GCash", "reference": payment_reference, "proof": self.proof()}
        with scanned_text(text):
            preview = self.customer_post(second, "payments/scan", payload)
        self.assertEqual(preview["verification"]["status"], "possible_duplicate")
        duplicate_check = next(item for item in preview["verification"]["checks"] if item["key"] == "duplicate")
        self.assertEqual(duplicate_check["state"], "inconsistent")
        self.assertIn("already submitted", duplicate_check["detail"].casefold())

        with scanned_text(text):
            self.customer_post(second, "payments", {**payload, "amount": 500}, expect=409)
        self.assertEqual(self.admin_view(second)["payment_submissions"], [])

    def test_same_receipt_file_after_request_is_still_flagged(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("SAMEFILE")
        shared_proof = self.proof()
        text = self.receipt_text(payment_reference, 500, "GCash")
        self.submit(reference, "GCash", payment_reference, text, proof=shared_proof)
        submission = self.pending_submission(reference)
        self.admin_post(reference, f"payment-submissions/{submission['id']}/request-receipt", {})

        with scanned_text(text):
            result = self.customer_post(reference, "payments/scan", {
                "method": "GCash",
                "reference": payment_reference,
                "proof": shared_proof,
            })
        self.assertEqual(result["verification"]["status"], "possible_duplicate")
        duplicate_check = next(item for item in result["verification"]["checks"] if item["key"] == "duplicate")
        self.assertEqual(duplicate_check["state"], "inconsistent")
        self.assertIn("same receipt file", duplicate_check["detail"].casefold())

    def test_gateway_endpoint_must_use_https_for_a_remote_host(self) -> None:
        methods = self.admin.get("/api/admin/settings/payment-methods").json()["methods"]
        method = next(item for item in methods if item["provider"] == "GCash")
        endpoint = f"/api/admin/settings/payment-methods/{method['id']}"
        try:
            response = self.admin.patch(endpoint, json={
                "verification_method": "api",
                "gateway_config": {
                    "provider": "Test Payment Gateway",
                    "endpoint": "http://gateway.example.test/verify",
                    "api_key": "super-secret-test-key",
                },
            })
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("https", response.json()["detail"].lower())

            local = self.admin.patch(endpoint, json={
                "verification_method": "api",
                "gateway_config": {
                    "provider": "Test Payment Gateway",
                    "endpoint": "http://127.0.0.1:9000/verify",
                    "api_key": "local-test-key",
                },
            })
            self.assertEqual(local.status_code, 200, local.text)
        finally:
            reset = self.admin.patch(endpoint, json={"verification_method": "manual", "gateway_config": {}})
            self.assertEqual(reset.status_code, 200, reset.text)

    def test_maribank_receipt_is_detected_and_stays_pending(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("MARI")
        result = self.scan(reference, "MariBank", payment_reference, self.receipt_text(payment_reference, 500, "MariBank"))

        self.assertEqual(result["method"], "MariBank")
        self.assertEqual(result["ocr"]["fields"]["method"], "MariBank")
        self.assertEqual(result["ocr_status"], "Detected")
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertFalse(result["duplicate_reference"])

    def test_scan_reports_duplicate_message_and_amount_match(self) -> None:
        first = self.place_order()
        payment_reference = unique_ref("DUPMSG")
        self.submit(first, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))

        second = self.place_order()
        result = self.scan(second, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertTrue(result["duplicate_reference"])
        self.assertEqual(
            result["duplicate_message"],
            "This reference number has already been submitted. Please check your payment details or upload a different transaction receipt.",
        )
        self.assertEqual(result["amount_match"], True)
        self.assertEqual(
            result["reference_message"],
            "This reference number has already been submitted. Please check your payment details or upload a different transaction receipt.",
        )

    def test_scan_reports_reference_detected_message_when_unique(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("UNIQ")
        result = self.scan(reference, "GCash", payment_reference, self.receipt_text(payment_reference, 500, "GCash"))
        self.assertFalse(result["duplicate_reference"])
        self.assertEqual(
            result["reference_message"],
            "Reference number detected. Please review your payment details before submitting.",
        )

    def test_multiple_reference_candidates_are_returned(self) -> None:
        reference = self.place_order()
        primary = unique_ref("REFA")
        secondary = unique_ref("REFB")
        text = "\n".join([
            "GCash payment receipt",
            f"Reference No.: {primary}",
            f"Transaction ID: {secondary}",
            f"Amount: PHP 500.00",
            f"Recipient: {METHOD_ACCOUNTS['GCash']}",
            f"Date: {date.today().isoformat()} 14:30",
            "Status: Success",
        ])
        result = self.scan(reference, "GCash", primary, text)
        candidates = result["ocr"].get("reference_candidates", [])
        values = [item.get("value") for item in candidates]
        self.assertIn(primary, values)
        self.assertIn(secondary, values)
        self.assertIn("multiple_reference_candidates", result["ocr"].get("flags", []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
