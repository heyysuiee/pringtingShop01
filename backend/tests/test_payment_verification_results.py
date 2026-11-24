"""End-to-end checks for the admin payment verification results shown in the payments table.

These tests focus on the customer-facing outcomes (insufficient, mismatch, duplicate, manual review,
verified, and accumulating partial payments) and on the server-calculated money figures. OCR is stubbed so
the suite never depends on Tesseract, and the provider API is stubbed so no network request is made.
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
from types import SimpleNamespace
from unittest.mock import patch


_test_directory = tempfile.TemporaryDirectory(prefix="impress-payment-results-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import create_session_token, SESSION_COOKIE  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import payment_method_repository  # noqa: E402
from backend.app.services import ocr_services  # noqa: E402
from backend.app.services import payment_verification_services as verification  # noqa: E402
from backend.app.services import report_services  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
ADMIN_EMAIL = "admin@example.test"
CUSTOMER_NAME = "Verification Result Customer"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
METHOD_ACCOUNTS = {
    "GCash": "Print Works GCash",
    "Maya": "Print Works Maya",
    "MariBank": "Print Works MariBank",
    "Bank Transfer": "Print Works Bank Account",
}


def unique_ref(prefix: str = "VR") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class StaticReceiptProvider(ocr_services.OCRProvider):
    name = "result-test-ocr"

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
def scanned_text(text: str | None = None):
    original = ocr_services._provider
    ocr_services.set_provider(StaticReceiptProvider(text or ""))
    try:
        yield
    finally:
        ocr_services.set_provider(original)


class PaymentVerificationResultsTests(unittest.TestCase):
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
                "account_number": "0917" + str(2000000 + len(provider)),
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

    def place_order(self, total: float = 1000, down_payment: float | None = None) -> str:
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
        quotation = {"total_amount": total, "payment_instructions": "Pay to the shop's e-wallet account."}
        if down_payment is not None:
            quotation["down_payment_amount"] = down_payment
        quoted = self.admin_post(reference, "quotation", quotation)
        self.customer_post(reference, "quotation/accept", {"quotation_revision": quoted["quotation"]["revision"]})
        return reference

    def proof(self, contents: bytes | None = None) -> dict:
        payload = contents or PNG_SIGNATURE + uuid.uuid4().bytes + b"0" * 32
        return {"name": "receipt.png", "dataUrl": "data:image/png;base64," + base64.b64encode(payload).decode()}

    def receipt_text(
        self,
        reference: str,
        amount: float,
        method: str = "GCash",
        *,
        recipient: str | None = None,
        sender: str = CUSTOMER_NAME,
    ) -> str:
        return "\n".join([
            f"{method} payment receipt",
            f"Reference: {reference}",
            f"Date: {date.today().isoformat()} 14:30",
            f"Sender: {sender}",
            f"Recipient: {recipient if recipient is not None else METHOD_ACCOUNTS[method]}",
            f"Payment method: {method}",
            f"Amount: PHP {amount:.2f}",
            "Status: Success",
        ])

    def scan(self, reference: str, payment_reference: str, text: str, *, proof: dict | None = None, method: str = "GCash") -> dict:
        with scanned_text(text):
            return self.customer_post(reference, "payments/scan", {
                "method": method, "reference": payment_reference, "proof": proof or self.proof(),
            })

    def submit(
        self,
        reference: str,
        payment_reference: str,
        text: str,
        *,
        amount: float = 500,
        proof: dict | None = None,
        method: str = "GCash",
        expect: int = 200,
    ) -> dict:
        with scanned_text(text):
            return self.customer_post(reference, "payments", {
                "amount": amount, "method": method, "reference": payment_reference, "proof": proof or self.proof(),
            }, expect)

    def pending_submission(self, reference: str) -> dict:
        return next(item for item in self.admin_view(reference)["payment_submissions"] if item["status"] == "pending")

    def verify_submission(self, reference: str, amount: float, submission_id: str | None = None) -> dict:
        submission_id = submission_id or self.pending_submission(reference)["id"]
        return self.admin_post(reference, f"payment-submissions/{submission_id}/verify", {
            "received": True, "amount": amount,
        })

    def payment_rows(self, reference: str, verification_filter: str = "") -> list[dict]:
        params = {"search": reference, "page_size": 50}
        if verification_filter:
            params["verification"] = verification_filter
        response = self.admin.get("/api/admin/payments", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def enable_provider_api(self, provider: str) -> None:
        methods = self.admin.get("/api/admin/settings/payment-methods").json()["methods"]
        method = next(item for item in methods if item["provider"] == provider)
        response = self.admin.patch(f"/api/admin/settings/payment-methods/{method['id']}", json={
            "verification_method": "api",
            "gateway_config": {
                "provider": "Test Payment Gateway",
                "endpoint": "https://provider.example.test/verify",
                "api_key": "super-secret-test-key",
                "timeout_seconds": 5,
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("super-secret-test-key", response.text)

    # ------------------------------------------------------------ outcomes

    def test_clean_receipt_is_ocr_checked_then_verified_by_an_admin(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("CLEAN")
        result = self.scan(reference, payment_reference, self.receipt_text(payment_reference, 500))

        preview = result["verification"]
        self.assertEqual(preview["status"], "pending")
        self.assertEqual(preview["result"], verification.RESULT_OCR_PENDING)
        self.assertEqual(preview["result_label"], "OCR-checked · pending manual verification")
        self.assertEqual(preview["verification_basis"], "ocr_only")
        self.assertFalse(preview["auto_confirm"])
        self.assertEqual(preview["down_payment"]["total_order_amount"], 1000.0)
        self.assertEqual(preview["down_payment"]["required_down_payment"], 500.0)
        self.assertEqual(preview["down_payment"]["actual_amount_paid"], 500.0)
        self.assertEqual(preview["down_payment"]["remaining_down_payment"], 0.0)
        self.assertEqual(preview["down_payment"]["payment_percentage"], 50.0)

        self.submit(reference, payment_reference, self.receipt_text(payment_reference, 500))
        self.assertEqual(self.admin_view(reference)["payments"], [])
        row = self.payment_rows(reference)[0]
        self.assertEqual(row["entry_type"], "submission")
        self.assertEqual(row["verification_result"], verification.RESULT_OCR_PENDING)
        self.assertEqual(row["verification_result_label"], "OCR-checked · pending manual verification")
        self.assertFalse(row["counts_as_paid"])
        self.assertEqual(row["actual_amount_paid"], 500.0)
        self.assertEqual(row["total_order_amount"], 1000.0)
        self.assertEqual(row["required_down_payment"], 500.0)
        self.assertEqual(row["remaining_down_payment"], 0.0)
        self.assertEqual(row["payment_percentage"], 50.0)
        self.assertEqual(row["ocr_reference"], payment_reference)
        self.assertTrue(row["receipt_url"])

        self.verify_submission(reference, 500)
        view = self.admin_view(reference)
        self.assertEqual(view["status"], "Confirmed")
        self.assertEqual(view["summary"]["amount_paid"], 500.0)
        rows = self.payment_rows(reference)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_type"], "payment")
        self.assertEqual(rows[0]["verification_result"], verification.RESULT_VERIFIED)
        self.assertEqual(rows[0]["verification_result_label"], "Verified")
        self.assertTrue(rows[0]["counts_as_paid"])
        self.assertTrue(rows[0]["verification_date"])

    def test_insufficient_payment_reports_the_shortfall_without_crediting_money(self) -> None:
        reference = self.place_order(total=2000, down_payment=1000)
        payment_reference = unique_ref("SHORT")
        text = self.receipt_text(payment_reference, 800)
        result = self.scan(reference, payment_reference, text)

        preview = result["verification"]
        self.assertEqual(preview["status"], "admin_review")
        self.assertEqual(preview["result"], verification.RESULT_INSUFFICIENT)
        self.assertEqual(preview["result_label"], "Insufficient Payment")
        self.assertFalse(preview["auto_confirm"])
        calculation = preview["down_payment"]
        self.assertEqual(calculation["total_order_amount"], 2000.0)
        self.assertEqual(calculation["required_down_payment"], 1000.0)
        self.assertEqual(calculation["stage_amount_due"], 1000.0)
        self.assertEqual(calculation["actual_amount_paid"], 800.0)
        self.assertEqual(calculation["insufficient_by"], 200.0)
        self.assertEqual(calculation["remaining_down_payment"], 200.0)
        self.assertEqual(calculation["payment_percentage"], 40.0)

        # The customer's form still submits the server-calculated amount due. OCR is what reports the short receipt;
        # nothing is credited until an administrator explicitly confirms the smaller amount actually received.
        self.submit(reference, payment_reference, text, amount=1000)
        self.assertEqual(self.admin_view(reference)["payments"], [])
        row = self.payment_rows(reference, verification.RESULT_INSUFFICIENT)[0]
        self.assertEqual(row["verification_result_label"], "Insufficient Payment")
        self.assertEqual(row["actual_amount_paid"], 800.0)
        self.assertEqual(row["required_down_payment"], 1000.0)
        self.assertEqual(row["remaining_down_payment"], 200.0)
        self.assertEqual(row["insufficient_by"], 200.0)
        self.assertFalse(row["counts_as_paid"])

    def test_customer_cannot_submit_below_the_remaining_down_payment(self) -> None:
        reference = self.place_order(total=2000, down_payment=1000)
        payment_reference = unique_ref("BELOW")
        text = self.receipt_text(payment_reference, 1000)
        too_low = self.submit(reference, payment_reference, text, amount=999.99, expect=422)
        self.assertIn("cannot be below the remaining down payment", too_low["detail"])
        self.assertIn("PHP 1,000.00", too_low["detail"])
        self.assertEqual(self.admin_view(reference)["payment_submissions"], [])

        # Exactly the down payment and a larger amount are both accepted; only the shop confirms the money.
        self.submit(reference, payment_reference, text, amount=1000)
        self.assertEqual(self.pending_submission(reference)["amount"], 1000.0)

        # A shop-recorded partial down payment lowers the customer minimum to what is still due.
        self.verify_submission(reference, 400)
        second_reference = unique_ref("ABOVE")
        second_text = self.receipt_text(second_reference, 700)
        still_low = self.submit(reference, second_reference, second_text, amount=599.99, expect=422)
        self.assertIn("PHP 600.00", still_low["detail"])
        self.submit(reference, second_reference, second_text, amount=700)
        self.assertEqual(self.pending_submission(reference)["amount"], 700.0)

    def test_reference_mismatch_is_flagged_and_never_auto_confirmed(self) -> None:
        reference = self.place_order()
        entered_reference = unique_ref("ENTERED")
        receipt_reference = unique_ref("RECEIPT")
        text = self.receipt_text(receipt_reference, 500)
        result = self.scan(reference, entered_reference, text)

        preview = result["verification"]
        self.assertEqual(preview["status"], "admin_review")
        self.assertEqual(preview["result"], verification.RESULT_REFERENCE_MISMATCH)
        self.assertEqual(preview["result_label"], "Reference Number Mismatch")
        self.assertFalse(preview["auto_confirm"])
        reference_check = next(check for check in preview["checks"] if check["key"] == "reference")
        self.assertEqual(reference_check["state"], "inconsistent")
        self.assertEqual(reference_check["expected"], entered_reference)
        self.assertEqual(reference_check["actual"], receipt_reference)

        self.submit(reference, entered_reference, text)
        self.assertEqual(self.admin_view(reference)["payments"], [])
        row = self.payment_rows(reference, verification.RESULT_REFERENCE_MISMATCH)[0]
        self.assertEqual(row["payment_reference"], entered_reference)
        self.assertEqual(row["ocr_reference"], receipt_reference)
        self.assertFalse(row["counts_as_paid"])

    def test_duplicate_reference_and_receipt_are_classified_and_filtered(self) -> None:
        first = self.place_order()
        first_reference = unique_ref("FIRST")
        shared_receipt = self.proof()
        self.submit(first, first_reference, self.receipt_text(first_reference, 500), proof=shared_receipt)
        self.verify_submission(first, 500)

        second = self.place_order()
        duplicate_reference = unique_ref("DUP")
        second_text = self.receipt_text(duplicate_reference, 500)
        result = self.scan(second, duplicate_reference, second_text, proof=shared_receipt)
        preview = result["verification"]
        self.assertEqual(preview["status"], "possible_duplicate")
        self.assertEqual(preview["result"], verification.RESULT_DUPLICATE)
        self.assertEqual(preview["result_label"], "Duplicate Reference Number")
        self.assertFalse(preview["auto_confirm"])
        self.assertTrue(result["duplicate_reference"])

        # The exact reference is claimed by the first order and cannot be submitted again.
        self.submit(second, first_reference, self.receipt_text(first_reference, 500), expect=409)
        self.assertEqual(self.admin_view(second)["payment_submissions"], [])

        # A new reference on the same receipt file is still a duplicate and is shown in the admin table.
        self.submit(second, duplicate_reference, second_text, proof=shared_receipt)
        self.assertEqual(self.admin_view(second)["payments"], [])
        row = self.payment_rows(second, verification.RESULT_DUPLICATE)[0]
        self.assertEqual(row["verification_result_label"], "Duplicate Reference Number")
        self.assertEqual(row["duplicate_reference_status"], "Duplicate detected")
        self.assertFalse(row["counts_as_paid"])

    def test_unreadable_receipt_is_needs_manual_review_and_never_counted(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("BLUR")
        result = self.scan(reference, payment_reference, "")

        preview = result["verification"]
        self.assertEqual(preview["status"], "admin_review")
        self.assertEqual(preview["result"], verification.RESULT_MANUAL_REVIEW)
        self.assertEqual(preview["result_label"], "Needs Manual Review")
        self.assertIsNone(preview["down_payment"]["actual_amount_paid"])
        self.assertEqual(preview["down_payment"]["remaining_down_payment"], 500.0)
        self.assertFalse(preview["auto_confirm"])

        self.submit(reference, payment_reference, "", amount=500)
        self.assertEqual(self.admin_view(reference)["payments"], [])
        row = self.payment_rows(reference, verification.RESULT_MANUAL_REVIEW)[0]
        self.assertEqual(row["ocr_amount"], None)
        self.assertEqual(row["ocr_reference"], "")
        self.assertEqual(row["ocr_detection_status"], "Failed")
        self.assertFalse(row["counts_as_paid"])

    def test_overpayment_reports_excess_without_crediting_it(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("EXCESS")
        text = self.receipt_text(payment_reference, 750)
        result = self.scan(reference, payment_reference, text)

        calculation = result["verification"]["down_payment"]
        self.assertEqual(calculation["actual_amount_paid"], 750.0)
        self.assertEqual(calculation["excess_amount"], 0.0)
        self.assertEqual(calculation["stage_excess_amount"], 250.0)
        self.assertEqual(calculation["payment_status"], "Partially Paid")
        self.assertEqual(calculation["insufficient_by"], 0.0)
        self.assertEqual(calculation["remaining_balance"], 250.0)
        self.assertEqual(calculation["payment_percentage"], 75.0)
        self.assertFalse(result["verification"]["auto_confirm"])

        self.submit(reference, payment_reference, text)
        self.assertEqual(self.admin_view(reference)["payments"], [])
        row = self.payment_rows(reference)[0]
        self.assertEqual(row["actual_amount_paid"], 750.0)
        self.assertEqual(row["excess_amount"], 0.0)
        self.assertEqual(row["payment_status_after"], "Partially Paid")

    def test_multiple_partial_payments_accumulate_and_never_credit_twice(self) -> None:
        reference = self.place_order(total=2000, down_payment=1000)

        first_reference = unique_ref("PART1")
        first_text = self.receipt_text(first_reference, 400)
        first_result = self.scan(reference, first_reference, first_text)
        self.assertEqual(first_result["verification"]["result"], verification.RESULT_INSUFFICIENT)
        self.assertEqual(first_result["verification"]["down_payment"]["insufficient_by"], 600.0)
        self.submit(reference, first_reference, first_text, amount=1000)
        self.verify_submission(reference, 400)
        after_first = self.admin_view(reference)
        self.assertEqual(after_first["summary"]["amount_paid"], 400.0)
        self.assertEqual(after_first["status"], "Awaiting Down Payment")

        second_reference = unique_ref("PART2")
        second_text = self.receipt_text(second_reference, 600)
        second_result = self.scan(reference, second_reference, second_text)
        preview = second_result["verification"]
        self.assertEqual(preview["result"], verification.RESULT_OCR_PENDING)
        calculation = preview["down_payment"]
        self.assertEqual(calculation["previously_paid"], 400.0)
        self.assertEqual(calculation["cumulative_paid"], 1000.0)
        self.assertEqual(calculation["remaining_down_payment"], 0.0)
        self.assertEqual(calculation["remaining_balance"], 1000.0)
        self.assertEqual(calculation["payment_percentage"], 50.0)
        self.assertTrue(calculation["down_payment_met"])

        self.submit(reference, second_reference, second_text, amount=600)
        second_submission_id = self.pending_submission(reference)["id"]
        self.verify_submission(reference, 600, second_submission_id)
        view = self.admin_view(reference)
        self.assertEqual(len(view["payments"]), 2)
        self.assertEqual(sum(payment["amount"] for payment in view["payments"]), 1000.0)
        self.assertEqual(view["summary"]["amount_paid"], 1000.0)
        self.assertEqual(view["status"], "Confirmed")

        rows = self.payment_rows(reference)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["actual_amount_paid"] for row in rows}, {400.0, 600.0})
        self.assertEqual({row["payment_percentage"] for row in rows}, {20.0, 50.0})
        self.assertTrue(all(row["counts_as_paid"] for row in rows))

        self.admin_post(reference, f"payment-submissions/{second_submission_id}/verify", {
            "received": True, "amount": 600,
        }, expect=409)
        self.assertEqual(len(self.admin_view(reference)["payments"]), 2)

    def test_three_payments_accumulate_to_a_1500_order(self) -> None:
        """₱750 down payment + ₱250 + ₱500: every confirmed payment adds to the total exactly once."""
        reference = self.place_order(total=1500)

        stages = (750, 250, 500)
        expected_statuses = ("Down Payment Paid", "Partially Paid", "Fully Paid")
        expected_balances = (750.0, 500.0, 0.0)
        for index, (amount, expected_status, expected_balance) in enumerate(
            zip(stages, expected_statuses, expected_balances), start=1
        ):
            payment_reference = unique_ref(f"STAGE{index}")
            self.submit(
                reference,
                payment_reference,
                self.receipt_text(payment_reference, amount),
                amount=amount,
            )
            submission_id = self.pending_submission(reference)["id"]
            view = self.verify_submission(reference, amount, submission_id)
            calculation = view["payment"]["payment_calculation"]
            self.assertEqual(calculation["confirmed_payments"], float(sum(stages[:index])))
            self.assertEqual(calculation["payment_status"], expected_status)
            self.assertEqual(calculation["balance_due"], expected_balance)
            self.assertEqual(calculation["excess_amount"], 0.0)

        view = self.admin_view(reference)
        self.assertEqual(len(view["payments"]), 3)
        self.assertEqual(sum(payment["amount"] for payment in view["payments"]), 1500.0)
        self.assertEqual(view["summary"]["amount_paid"], 1500.0)
        self.assertEqual(view["payment"]["amount_paid"], 1500.0)
        self.assertEqual(view["payment"]["payment_calculation"]["remaining_balance_exact"], 0.0)

        rows = self.payment_rows(reference)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row["actual_amount_paid"] for row in rows), [250.0, 500.0, 750.0])
        self.assertTrue(all(row["counts_as_paid"] for row in rows))

        # Confirming the same submission twice must never credit the payment again.
        self.admin_post(reference, f"payment-submissions/{submission_id}/verify", {
            "received": True, "amount": 750,
        }, expect=409)
        after_duplicate = self.admin_view(reference)
        self.assertEqual(len(after_duplicate["payments"]), 3)
        self.assertEqual(after_duplicate["summary"]["amount_paid"], 1500.0)

    def test_recorded_row_counts_the_confirmed_amount_not_the_ocr_reading(self) -> None:
        reference = self.place_order()
        payment_reference = unique_ref("OCRDIFF")
        text = self.receipt_text(payment_reference, 750)
        self.submit(reference, payment_reference, text, amount=750)
        pending_row = self.payment_rows(reference)[0]
        self.assertEqual(pending_row["actual_amount_paid"], 750.0)  # while pending, the OCR reading is shown
        # OCR read 750, but the administrator confirms only 600 was actually received.
        self.verify_submission(reference, 600)
        row = self.payment_rows(reference)[0]
        self.assertEqual(row["entry_type"], "payment")
        self.assertEqual(row["ocr_amount"], 750.0)
        self.assertEqual(row["amount"], 600.0)
        self.assertEqual(row["actual_amount_paid"], 600.0)
        self.assertEqual(row["payment_percentage"], 60.0)
        # The table's own summary counts the same confirmed amount, not the receipt reading.
        summary = self.admin.get("/api/admin/payments", params={"search": reference, "page_size": 50}).json()["summary"]
        self.assertEqual(summary["filtered_amount"], 600.0)
        self.assertEqual(summary["filtered_count"], 1)

    def test_payment_summary_splits_at_the_admin_set_down_payment(self) -> None:
        reference = self.place_order(total=1000, down_payment=700)
        payment_reference = unique_ref("DOWN70")
        self.submit(reference, payment_reference, self.receipt_text(payment_reference, 700), amount=700)
        self.verify_submission(reference, 700)
        summary = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((summary["received_total"], summary["down_payments"], summary["balance_payments"]), (700.0, 700.0, 0.0))
        self.admin_post(reference, "payments", {"amount": 300, "method": "Cash", "received": True})
        summary = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((summary["received_total"], summary["down_payments"], summary["balance_payments"]), (1000.0, 700.0, 300.0))

    def test_payment_status_filters_accept_every_reported_status(self) -> None:
        statuses = ("Not Quoted", "No Payment", "Insufficient Down Payment", "Down Payment Paid", "Partially Paid", "Fully Paid", "Excess Payment")
        for status in statuses:
            completed = self.admin.get("/api/admin/reports/completed", params={"payment_status": status})
            self.assertEqual(completed.status_code, 200, completed.text)
            payments = self.admin.get("/api/admin/payments", params={"payment_status": status})
            self.assertEqual(payments.status_code, 200, payments.text)
        stale = self.admin.get("/api/admin/reports/completed", params={"payment_status": "Unpaid"})
        self.assertIn(stale.status_code, (400, 422))

    def test_customer_excess_is_flagged_for_review_without_crediting_money(self) -> None:
        reference = self.place_order(total=1500)
        payment_reference = unique_ref("CUSTEX")
        declared = self.submit(
            reference,
            payment_reference,
            self.receipt_text(payment_reference, 1600),
            amount=1600,
        )
        submission = next(item for item in declared["payment_submissions"] if item["reference"] == payment_reference)
        self.assertEqual(submission["amount"], 1600.0)
        self.assertEqual(submission["status"], "pending")
        self.assertEqual(submission["excess_amount"], 100.0)
        self.assertTrue(submission["requires_admin_review"])
        self.assertEqual(submission["verification_status"], "admin_review")
        self.assertFalse(submission["verification"]["auto_confirm"])

        self.assertEqual(declared["payments"], [])
        self.assertEqual(declared["payment"]["amount_paid"], 0.0)
        self.assertEqual(declared["payment"]["payment_status"], "No Payment")

    def test_positive_provider_confirmation_is_verified_and_credited_once(self) -> None:
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
            result = self.scan(reference, payment_reference, self.receipt_text(payment_reference, 500))
        preview = result["verification"]
        self.assertEqual(preview["status"], "verified")
        self.assertEqual(preview["result"], verification.RESULT_VERIFIED)
        self.assertEqual(preview["result_label"], "Verified")
        self.assertEqual(preview["verification_basis"], "provider_api")
        self.assertTrue(preview["auto_confirm"])

        with patch("httpx.post", side_effect=confirmed_provider):
            self.submit(reference, payment_reference, self.receipt_text(payment_reference, 500))
        view = self.admin_view(reference)
        self.assertEqual(len(view["payments"]), 1)
        self.assertEqual(view["status"], "Confirmed")
        row = self.payment_rows(reference)[0]
        self.assertEqual(row["verification_result"], verification.RESULT_VERIFIED)
        self.assertEqual(row["verification_basis_label"], "Independently confirmed by the payment provider")
        self.assertTrue(row["counts_as_paid"])

        with patch("httpx.post", side_effect=confirmed_provider):
            self.submit(reference, payment_reference, self.receipt_text(payment_reference, 500), expect=409)
        self.assertEqual(len(self.admin_view(reference)["payments"]), 1)


class DownPaymentCalculationTests(unittest.TestCase):
    def test_default_and_admin_set_amounts_are_both_recomputed_on_the_server(self) -> None:
        default = verification.down_payment_calculation({"total_amount": 1000}, 500, 500, 0)
        self.assertEqual(default["required_down_payment"], 500.0)
        self.assertEqual(default["remaining_down_payment"], 0.0)
        self.assertEqual(default["remaining_balance"], 500.0)
        self.assertTrue(default["down_payment_met"])

        admin_set = verification.down_payment_calculation(
            {"total_amount": 2000, "required_down_payment": 1200}, 1200, 800, 400
        )
        self.assertEqual(admin_set["total_order_amount"], 2000.0)
        self.assertEqual(admin_set["required_down_payment"], 1200.0)
        self.assertEqual(admin_set["previously_paid"], 400.0)
        self.assertEqual(admin_set["cumulative_paid"], 1200.0)
        self.assertEqual(admin_set["remaining_down_payment"], 0.0)
        self.assertEqual(admin_set["remaining_balance"], 800.0)
        self.assertEqual(admin_set["payment_percentage"], 60.0)
        self.assertEqual(admin_set["insufficient_by"], 400.0)
        self.assertTrue(admin_set["down_payment_met"])

    def test_unreadable_amount_keeps_the_full_down_payment_open(self) -> None:
        calculation = verification.down_payment_calculation({"total_amount": 1000}, 500, None, 0)
        self.assertIsNone(calculation["actual_amount_paid"])
        self.assertEqual(calculation["remaining_down_payment"], 500.0)
        self.assertEqual(calculation["insufficient_by"], 0.0)
        self.assertEqual(calculation["excess_amount"], 0.0)
        self.assertFalse(calculation["down_payment_met"])

    def test_1500_order_status_ladder_uses_cumulative_confirmed_money(self) -> None:
        cases = (
            # (received so far, amount on this receipt, stage due, status, balance, exact balance, excess)
            (500, 500, 750, "Insufficient Down Payment", 1000.0, 1000.0, 0.0),
            (750, 250, 250, "Down Payment Paid", 750.0, 750.0, 0.0),
            (1000, 250, 750, "Partially Paid", 500.0, 500.0, 0.0),
            (1500, 500, 500, "Fully Paid", 0.0, 0.0, 0.0),
            (1750, 250, 0, "Excess Payment", 0.0, -250.0, 250.0),
        )
        for cumulative, amount, stage_due, status, balance, exact_balance, excess in cases:
            with self.subTest(cumulative=cumulative):
                calculation = verification.down_payment_calculation(
                    {"total_amount": 1500}, stage_due, amount, cumulative - amount
                )
                self.assertEqual(calculation["cumulative_paid"], float(cumulative))
                self.assertEqual(calculation["payment_status"], status)
                self.assertEqual(calculation["remaining_balance"], balance)
                self.assertEqual(calculation["remaining_balance_exact"], exact_balance)
                self.assertEqual(calculation["excess_amount"], excess)
                self.assertTrue(calculation["down_payment_met"] is (cumulative >= 750))
        self.assertEqual(
            verification.down_payment_calculation({"total_amount": 1500}, 0, 250, 1500)["stage_excess_amount"],
            250.0,
        )


class AdminPaymentVerificationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2]
        cls.dashboard_js = (root / "frontend" / "assets" / "admin" / "dashboard.js").read_text(encoding="utf-8")
        cls.dashboard_css = (root / "frontend" / "assets" / "admin" / "dashboard.css").read_text(encoding="utf-8")

    def test_clicking_a_payment_row_opens_the_verification_modal(self) -> None:
        self.assertIn("onRowClick: (row) => openPaymentVerification(row, () => table.reload())", self.dashboard_js)
        for marker in (
            "async function openPaymentVerification(row, reload)",
            "Original receipt",
            "Extracted payment information",
            "Automatic down payment calculation",
            "verification-modal-grid",
            "verificationReceipt(",
        ):
            self.assertIn(marker, self.dashboard_js)

    def test_payment_table_and_modal_keep_every_required_verification_fact(self) -> None:
        # The table stays decision-focused; the full verification panel remains authoritative.
        for label in (
            "Action", "Date", "Order ID", "Customer", "Method", "Reference no.",
            "OCR (assist only)", "Amount counted", "Verification", "Order balance",
        ):
            self.assertIn(f'label: "{label}"', self.dashboard_js)
        for fact in (
            "Extracted reference", "Actual amount paid", "Total order amount", "Required down payment",
            "Remaining down payment", "Payment %", "Verification date", "Verification status",
        ):
            self.assertIn(f'"{fact}"', self.dashboard_js)
        for value in (
            "ocr_checked_pending", "insufficient_payment", "reference_mismatch",
            "duplicate_reference", "needs_manual_review",
        ):
            self.assertIn(f'["{value}"', self.dashboard_js)

    def test_verification_modal_has_its_own_styles(self) -> None:
        for selector in (".verification-modal-grid", ".verify-pane", ".verify-calculation"):
            self.assertIn(selector, self.dashboard_css)

    def test_payment_status_filters_offer_every_reported_status(self) -> None:
        # The filter vocabulary matches the statuses the tables and reports actually show.
        self.assertIn(
            'const PAYMENT_STATUSES = ["Not Quoted", "No Payment", "Insufficient Down Payment", "Down Payment Paid", "Partially Paid", "Fully Paid", "Excess Payment"];',
            self.dashboard_js,
        )
        self.assertNotIn("PAYMENT_STATUSES.slice", self.dashboard_js)
        self.assertNotIn('"Unpaid"', self.dashboard_js)
        # The amount counted column shows the confirmed amount, matching the table summary and exports.
        self.assertIn("text: money(row.amount)", self.dashboard_js)


if __name__ == "__main__":
    unittest.main()
