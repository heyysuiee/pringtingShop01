"""Order, quotation, fulfillment, and payment workflow (package and customized orders)."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-workflow-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
# Never read the developer's .env file (SMTP credentials, production mode) during tests.
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import order_repository  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
MUG_OPTIONS = {"size": "11-oz", "print_area": "one-side", "inner_color": "white-classic", "packaging": "no-packaging"}
BIRTHDAY_PACKAGE_OPTIONS = {
    "paper_finish": "glossy",
    "envelope_option": "plain-white-envelope",
    "celebrant_name": "Mia Santos",
    "celebrant_age": 7,
    "event_date": "2026-12-12",
    "event_time": "14:00",
    "event_location": "Quezon City",
}
DELIVERY = {
    "method": "delivery",
    "recipient_name": "Ana Reyes",
    "contact_number": "0917 555 1234",
    "street": "12 Mabini St.",
    "barangay": "San Roque",
    "city": "Marikina City",
    "province": "Metro Manila",
    "landmark": "Across the chapel",
}


def idempotency_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def unique_reference() -> str:
    return f"GC-{uuid.uuid4().hex[:12].upper()}"


class WorkflowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()
        self.admin = TestClient(app)
        self.admin.cookies.set(SESSION_COOKIE, create_session_token("admin@example.test", "admin"))
        self.shopper = TestClient(app)
        response = self.shopper.post(
            "/api/customer/register",
            json={
                "name": "Workflow Customer",
                "email": f"{uuid.uuid4().hex[:12]}@example.test",
                "phone": "+63 917 123 4567",
                "messenger": "",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.email = response.json()["customer"]["email"]

    # ------------------------------------------------------------ helpers

    def place(self, product_id: str = "white-mug", quantity: int = 2, options: dict | None = None, **extra) -> dict:
        customization = {"quantity": quantity, "options": options or MUG_OPTIONS, "notes": ""}
        if "package_id" in extra:
            customization["package_id"] = extra.pop("package_id")
        payload = {
            "customer": {"name": "Workflow Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": customization,
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": [],
            **extra,
        }
        response = self.shopper.post("/api/orders", json=payload, headers={"Idempotency-Key": idempotency_key()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def admin_view(self, reference: str) -> dict:
        response = self.admin.get(f"/api/admin/orders/{reference}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def admin_post(self, reference: str, action: str, body: dict | None = None, expect: int = 200) -> dict:
        body = {**(body or {})}
        body.setdefault("expected_version", self.admin_view(reference)["version"])
        response = self.admin.post(f"/api/admin/orders/{reference}/{action}", json=body)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def customer_post(self, reference: str, action: str, body: dict | None = None, expect: int = 200) -> dict:
        response = self.shopper.post(f"/api/orders/{reference}/{action}", json={"email": self.email, **(body or {})})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def customized_order(self, amount: float = 1000, cod_allowed: bool = False, **extra) -> dict:
        order = self.place(**extra)
        reference = order["reference"]
        self.admin_post(reference, "status", {"status": "Under Review"})
        return self.admin_post(reference, "quotation", {"total_amount": amount, "cod_allowed": cod_allowed, "payment_instructions": "GCash 0917 000 0000"})

    def accept(self, order: dict, option: str, expect: int = 200) -> dict:
        return self.customer_post(
            order["reference"],
            "quotation/accept",
            {"payment_option": option, "quotation_revision": order["quotation"]["revision"]},
            expect,
        )

    def submit_payment(self, reference: str, amount: float, payment_reference: str | None = None, proof: bool = True, expect: int = 200) -> dict:
        body = {"amount": amount, "method": "GCash", "reference": payment_reference or unique_reference()}
        if proof:
            body["proof"] = {"name": "receipt.png", "dataUrl": PNG}
        return self.customer_post(reference, "payments", body, expect)

    def pending_submission(self, reference: str) -> dict:
        return next(item for item in self.admin_view(reference)["payment_submissions"] if item["status"] == "pending")

    def verify(self, reference: str, expect: int = 200, **body) -> dict:
        submission = self.pending_submission(reference)
        return self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {"received": True, **body}, expect)

    def through_draft_approval(self, reference: str) -> dict:
        self.admin_post(reference, "draft", {"file": {"name": "draft.png", "dataUrl": PNG}})
        return self.customer_post(reference, "approve")


class FixedPricePackageTests(WorkflowTestCase):
    def test_package_order_uses_the_published_price_and_calculates_the_down_payment(self) -> None:
        order = self.place("birthday-invitation", 25, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="down_payment")
        # ₱300 for 20 pcs + 5 × ₱15 add-on: the published package total is the order total.
        self.assertTrue(order["fixed_price"])
        self.assertEqual(order["total_amount"], 375.0)
        self.assertEqual(order["payment"]["payment_option"], "down_payment")
        self.assertEqual(order["payment"]["down_payment_required"], 187.5)
        self.assertEqual(order["payment"]["remaining_after_down_payment"], 187.5)
        self.assertEqual(order["status"], "Pending Confirmation")
        reference = order["reference"]

        # Nothing can be paid before the shop confirms the order.
        self.submit_payment(reference, 187.5, expect=409)
        self.admin_post(reference, "payments", {"amount": 187.5, "method": "GCash", "received": True}, expect=409)
        # A package is never re-priced through a quotation.
        self.admin_post(reference, "quotation", {"total_amount": 999}, expect=409)
        view = self.admin_view(reference)
        self.assertTrue(view["actions"]["confirm_package"])
        self.assertFalse(view["actions"]["quote"])
        self.assertNotIn("Awaiting Quotation", self.admin_view(reference)["allowed_statuses"])

        confirmed = self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000 (Print Works)"})
        self.assertEqual(confirmed["status"], "Awaiting Down Payment")
        self.assertEqual(confirmed["payment_instructions"], "GCash 0917 000 0000 (Print Works)")
        self.submit_payment(reference, 187.5)
        verified = self.verify(reference)
        self.assertEqual(verified["status"], "Draft Preparation")
        self.assertEqual(verified["payment"]["down_payment_status"], "Paid")
        self.assertEqual(verified["payment"]["balance"], 187.5)

    def test_package_cash_on_delivery_skips_the_down_payment(self) -> None:
        order = self.place("birthday-invitation", 20, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="cod")
        self.assertEqual(order["payment"]["down_payment_required"], 0.0)
        self.assertEqual(order["payment"]["remaining_after_down_payment"], 300.0)
        confirmed = self.admin_post(order["reference"], "confirm")
        self.assertEqual(confirmed["status"], "Draft Preparation")
        self.assertTrue(self.admin_view(order["reference"])["actions"]["send_draft"])
        # COD customers do not upload a down-payment receipt.
        self.submit_payment(order["reference"], 100, expect=409)


class CustomizedQuotationTests(WorkflowTestCase):
    def test_customized_order_waits_for_a_quotation_before_any_payment(self) -> None:
        order = self.place(payment_option="cod")
        reference = order["reference"]
        self.assertFalse(order["fixed_price"])
        self.assertIsNone(order["total_amount"])
        self.assertEqual(order["payment_option"], "", "Customized orders choose a payment option after the quotation.")
        self.submit_payment(reference, 100, expect=409)
        self.admin_post(reference, "status", {"status": "Under Review"})
        self.admin_post(reference, "payments", {"amount": 100, "method": "Cash", "received": True}, expect=409)
        self.admin_post(reference, "confirm", expect=409)

        quoted = self.admin_post(reference, "quotation", {"total_amount": 1200, "cod_allowed": False, "note": "Includes layout"})
        self.assertEqual(quoted["status"], "Quotation Sent")
        self.assertEqual(quoted["quotation"]["amount"], 1200.0)
        self.assertEqual(quoted["quotation"]["note"], "Includes layout")
        # Sent but not accepted: still no payments.
        self.submit_payment(reference, 600, expect=409)
        self.admin_post(reference, "payments", {"amount": 600, "method": "Cash", "received": True}, expect=409)

    def test_cod_is_refused_unless_the_admin_allows_it(self) -> None:
        quoted = self.customized_order(800, cod_allowed=False)
        self.assertFalse(quoted["payment"]["cod_allowed"])
        self.accept(quoted, "cod", expect=422)
        accepted = self.accept(quoted, "down_payment")
        self.assertEqual(accepted["status"], "Awaiting Down Payment")
        self.assertEqual(accepted["payment"]["down_payment_required"], 400.0)
        self.assertEqual(accepted["quotation"]["status"], "accepted")

    def test_allowed_cod_needs_admin_confirmation_before_the_draft(self) -> None:
        quoted = self.customized_order(800, cod_allowed=True)
        accepted = self.accept(quoted, "cod")
        reference = accepted["reference"]
        self.assertEqual(accepted["status"], "Awaiting COD Confirmation")
        self.assertFalse(self.admin_view(reference)["actions"]["send_draft"])
        self.admin_post(reference, "draft", {"file": {"name": "draft.png", "dataUrl": PNG}}, expect=409)
        confirmed = self.admin_post(reference, "confirm-cod")
        self.assertEqual(confirmed["status"], "Draft Preparation")
        self.assertTrue(confirmed["quotation"]["cod_confirmed_at"])

    def test_admin_can_withdraw_cod_by_revising_the_quotation(self) -> None:
        quoted = self.customized_order(800, cod_allowed=True)
        reference = self.accept(quoted, "cod")["reference"]
        revised = self.admin_post(reference, "quotation", {"total_amount": 800, "cod_allowed": False, "note": "COD unavailable for this area"})
        self.assertEqual((revised["status"], revised["quotation"]["revision"]), ("Quotation Sent", 2))
        self.assertEqual(revised["payment_option"], "")
        self.accept(revised, "cod", expect=422)
        self.assertEqual(self.accept(revised, "down_payment")["status"], "Awaiting Down Payment")

    def test_customer_cannot_accept_an_outdated_quotation(self) -> None:
        first = self.customized_order(500)
        self.admin_post(first["reference"], "quotation", {"total_amount": 650})
        self.accept(first, "down_payment", expect=409)
        latest = self.admin_view(first["reference"])
        self.assertEqual(self.accept(latest, "down_payment")["total_amount"], 650.0)

    def test_declined_quotation_can_be_revised_and_accepted(self) -> None:
        quoted = self.customized_order(1500)
        reference = quoted["reference"]
        declined = self.customer_post(reference, "quotation/decline", {"quotation_revision": 1, "reason": "Over budget"})
        self.assertEqual(declined["status"], "Quotation Declined")
        self.assertEqual(declined["quotation"]["decline_reason"], "Over budget")
        self.accept(declined, "down_payment", expect=409)
        revised = self.admin_post(reference, "quotation", {"total_amount": 1300})
        accepted = self.accept(revised, "down_payment")
        self.assertEqual((accepted["status"], accepted["total_amount"]), ("Awaiting Down Payment", 1300.0))

    def test_quotation_is_locked_once_money_is_reported(self) -> None:
        quoted = self.customized_order(1000)
        reference = self.accept(quoted, "down_payment")["reference"]
        self.submit_payment(reference, 500)
        self.admin_post(reference, "quotation", {"total_amount": 900}, expect=409)
        self.assertFalse(self.admin_view(reference)["actions"]["quote"])


class PaymentVerificationTests(WorkflowTestCase):
    def test_submitted_payment_counts_only_after_verification(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        submitted = self.submit_payment(reference, 500)
        self.assertEqual(submitted["payment"]["amount_paid"], 0.0)
        self.assertEqual(submitted["payment"]["pending_verification"], 500.0)
        self.assertEqual(submitted["payment"]["down_payment_status"], "Awaiting verification")
        self.assertEqual(submitted["status"], "Awaiting Down Payment")
        proof_url = submitted["payment_submissions"][0]["proof"]["url"]
        self.assertEqual(self.shopper.get(proof_url).status_code, 200)
        view = self.admin_view(reference)
        self.assertTrue(view["actions"]["verify_payments"])
        self.assertFalse(view["actions"]["record_payment"])
        admin_proof = view["payment_submissions"][0]["proof"]["url"]
        self.assertEqual(self.admin.get(admin_proof).status_code, 200)

        verified = self.verify(reference)
        self.assertEqual(verified["status"], "Draft Preparation")
        self.assertEqual(verified["payment"]["amount_paid"], 500.0)
        self.assertEqual(verified["payments"][0]["kind"], "Down payment")
        self.assertEqual(verified["payment_submissions"][0]["status"], "verified")

    def test_duplicate_submissions_and_records_are_refused(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        gcash = unique_reference()
        self.submit_payment(reference, 500, gcash)
        # One submission waits at a time; the admin cannot record the same money separately meanwhile.
        self.submit_payment(reference, 500, expect=409)
        self.admin_post(reference, "payments", {"amount": 500, "method": "GCash", "received": True}, expect=409)
        submission = self.pending_submission(reference)
        self.verify(reference)
        # A verified submission cannot be verified again.
        self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {"received": True}, expect=409)
        # The same transaction reference cannot be reused on this or any other order.
        self.submit_payment(reference, 100, gcash.lower(), expect=409)
        other = self.accept(self.customized_order(1000), "down_payment")["reference"]
        self.submit_payment(other, 500, gcash, expect=409)
        self.admin_post(other, "payments", {"amount": 500, "method": "GCash", "reference": gcash, "received": True}, expect=409)
        stored = order_repository.get_order(reference)
        self.assertEqual(len(stored["payments"]), 1)

    def test_rejected_submission_can_be_resubmitted(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        gcash = unique_reference()
        self.submit_payment(reference, 500, gcash)
        submission = self.pending_submission(reference)
        self.admin_post(reference, f"payment-submissions/{submission['id']}/reject", {"note": ""}, expect=422)
        rejected = self.admin_post(reference, f"payment-submissions/{submission['id']}/reject", {"note": "Not found in GCash"})
        self.assertEqual(rejected["payment_submissions"][0]["status"], "rejected")
        self.assertEqual(rejected["payment"]["pending_verification"], 0.0)
        self.assertEqual(rejected["status"], "Awaiting Down Payment")
        self.submit_payment(reference, 500, gcash)

    def test_amount_cannot_exceed_the_balance_and_admin_can_correct_it(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        self.submit_payment(reference, 1000.01, expect=422)
        self.submit_payment(reference, 600)
        self.verify(reference, expect=422, received=False)
        verified = self.verify(reference, amount=550)
        self.assertEqual(verified["payment"]["amount_paid"], 550.0)
        self.assertIn("customer reported PHP 600.00", verified["history"][-1]["text"])

    def test_non_finite_amounts_are_rejected(self) -> None:
        # Python's JSON parser accepts NaN/Infinity; they must never become a payment amount.
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        headers = {"Content-Type": "application/json"}
        for literal in ("NaN", "Infinity", "-Infinity"):
            submitted = self.shopper.post(
                f"/api/orders/{reference}/payments",
                content=f'{{"email": "{self.email}", "amount": {literal}, "method": "GCash", "reference": "{unique_reference()}"}}',
                headers=headers,
            )
            self.assertEqual(submitted.status_code, 422, f"customer {literal}: {submitted.text}")
            version = self.admin_view(reference)["version"]
            recorded = self.admin.post(
                f"/api/admin/orders/{reference}/payments",
                content=f'{{"amount": {literal}, "method": "Cash", "received": true, "expected_version": {version}}}',
                headers=headers,
            )
            self.assertEqual(recorded.status_code, 422, f"admin {literal}: {recorded.text}")
        self.submit_payment(reference, 500)
        submission = self.pending_submission(reference)
        verified = self.admin.post(
            f"/api/admin/orders/{reference}/payment-submissions/{submission['id']}/verify",
            content=f'{{"amount": NaN, "received": true, "expected_version": {self.admin_view(reference)["version"]}}}',
            headers=headers,
        )
        self.assertEqual(verified.status_code, 422, verified.text)
        self.assertEqual(order_repository.get_order(reference)["payments"], [])

    def test_customer_cannot_cancel_with_a_pending_payment(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        self.submit_payment(reference, 500)
        self.customer_post(reference, "cancel", expect=409)
        self.admin_post(reference, "status", {"status": "Cancelled"}, expect=409)


class ProductionAndFulfillmentTests(WorkflowTestCase):
    def test_delivery_details_are_required_and_stored(self) -> None:
        incomplete = {**DELIVERY, "barangay": ""}
        payload_base = {
            "customer": {"name": "Workflow Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": "white-mug"},
            "customization": {"quantity": 1, "options": MUG_OPTIONS, "notes": ""},
            "event": {},
            "has_event": False,
            "acknowledged": True,
        }
        for fulfillment in (incomplete, {"method": "drone"}, {**DELIVERY, "contact_number": "abc"}):
            response = self.shopper.post("/api/orders", json={**payload_base, "fulfillment": fulfillment}, headers={"Idempotency-Key": idempotency_key()})
            self.assertEqual(response.status_code, 422, response.text)
        order = self.place(fulfillment=DELIVERY)
        self.assertEqual(order["fulfillment"]["method"], "delivery")
        address = {row["key"]: row["value"] for row in order["fulfillment"]["address"]}
        self.assertEqual(address["barangay"], "San Roque")
        self.assertEqual(address["landmark"], "Across the chapel")
        self.assertEqual(order["fulfillment"]["release_status"], "Out for Delivery")
        pickup = self.place(fulfillment={"method": "pickup", "street": "ignored"})
        self.assertEqual((pickup["fulfillment"]["method"], pickup["fulfillment"]["address"]), ("pickup", []))
        stored = order_repository.get_order(pickup["reference"])
        self.assertEqual(stored["fulfillment"], {"method": "pickup"})

    def test_fulfillment_is_independent_of_the_payment_option(self) -> None:
        cod_pickup = self.place("birthday-invitation", 20, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="cod", fulfillment={"method": "pickup"})
        dp_delivery = self.place("birthday-invitation", 20, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="down_payment", fulfillment=DELIVERY)
        self.assertEqual((cod_pickup["payment_option"], cod_pickup["fulfillment"]["method"]), ("cod", "pickup"))
        self.assertEqual((dp_delivery["payment_option"], dp_delivery["fulfillment"]["method"]), ("down_payment", "delivery"))

    def test_full_delivery_flow_with_balance_collection(self) -> None:
        reference = self.accept(self.customized_order(1000, fulfillment=DELIVERY), "down_payment")["reference"]
        self.submit_payment(reference, 500)
        self.verify(reference)
        # No production before the customer approves a draft.
        self.admin_post(reference, "status", {"status": "In Production"}, expect=409)
        self.admin_post(reference, "draft", {"file": {"name": "draft-1.png", "dataUrl": PNG}})
        revision = self.customer_post(reference, "request-changes", {"note": "Bigger name please"})
        self.assertEqual(revision["status"], "Draft Preparation")
        approved = self.through_draft_approval(reference)
        self.assertEqual(approved["status"], "Approved for Printing")
        self.assertIn("revised draft", approved["history"][-2]["text"])
        self.admin_post(reference, "status", {"status": "In Production"})
        view = self.admin_view(reference)
        self.assertEqual(view["allowed_statuses"], ["Out for Delivery"])
        self.admin_post(reference, "status", {"status": "Ready for Pickup"}, expect=409)
        self.admin_post(reference, "status", {"status": "Out for Delivery"})
        # Not completed while a balance is outstanding.
        self.assertTrue(self.admin_view(reference)["actions"]["complete_requires_payment"])
        self.admin_post(reference, "status", {"status": "Completed"}, expect=409)
        collected = self.admin_post(reference, "payments", {"amount": 500, "method": "Cash", "received": True})
        self.assertEqual(collected["payments"][-1]["kind"], "Balance payment")
        completed = self.admin_post(reference, "status", {"status": "Completed"})
        self.assertEqual(completed["status"], "Completed")
        self.assertEqual(completed["payment"]["balance"], 0.0)

    def test_cod_pickup_collects_the_full_amount_before_completion(self) -> None:
        order = self.place("birthday-invitation", 20, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="cod")
        reference = order["reference"]
        self.admin_post(reference, "confirm")
        self.through_draft_approval(reference)
        self.admin_post(reference, "status", {"status": "In Production"})
        self.assertEqual(self.admin_view(reference)["allowed_statuses"], ["Ready for Pickup"])
        self.admin_post(reference, "status", {"status": "Ready for Pickup"})
        self.admin_post(reference, "status", {"status": "Completed"}, expect=409)
        collected = self.admin_post(reference, "payments", {"amount": 300, "method": "Cash on Delivery", "received": True})
        self.assertEqual(collected["payments"][0]["kind"], "COD collection")
        self.assertEqual(self.admin_post(reference, "status", {"status": "Completed"})["status"], "Completed")

    def test_production_needs_the_down_payment_even_for_older_records(self) -> None:
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        # Simulate an inconsistent older record: approved for printing without the verified down payment.
        with connection_scope() as connection:
            connection.execute(
                "UPDATE orders SET status = 'Approved for Printing', draft_attachment = ? WHERE reference = ?",
                (json.dumps({"id": "d1", "name": "d.png", "filename": "x.png", "mime_type": "image/png", "size": 1}), reference),
            )
        self.assertTrue(self.admin_view(reference)["actions"]["start_production_blocked"])
        self.admin_post(reference, "status", {"status": "In Production"}, expect=409)


class AdminOverviewTests(WorkflowTestCase):
    def test_badges_and_payment_page_show_work_waiting_for_the_admin(self) -> None:
        before = self.admin.get("/api/admin/counts").json()
        reference = self.accept(self.customized_order(1000), "down_payment")["reference"]
        self.submit_payment(reference, 500)
        after = self.admin.get("/api/admin/counts").json()
        self.assertEqual(after["payments_to_verify"], before["payments_to_verify"] + 1)
        payments = self.admin.get("/api/admin/payments").json()
        self.assertIn(reference, [item["reference"] for item in payments["summary"]["payments_to_verify"]])
        listing = self.admin.get("/api/admin/orders", params={"search": reference}).json()["items"][0]
        self.assertEqual(listing["payments_to_verify"], 1)
        self.assertEqual(listing["fulfillment_label"], "Store Pickup")
        activity = self.admin.get("/api/admin/activity", params={"limit": 50}).json()["events"]
        titles = {event["title"] for event in activity if event["reference"] == reference}
        self.assertTrue({"Quotation sent", "Quotation accepted", "Payment submitted for verification"} <= titles)

    def test_unagreed_prices_are_not_counted_as_owed(self) -> None:
        def customer_row() -> dict:
            return self.admin.get("/api/admin/customers", params={"search": self.email}).json()["items"][0]

        quoted = self.customized_order(1000)
        package = self.place("birthday-invitation", 20, BIRTHDAY_PACKAGE_OPTIONS, package_id="birthday-set-a", payment_option="cod")
        # A quotation the customer has not accepted and a package the shop has not confirmed are offers, not debts.
        row = customer_row()
        self.assertEqual((row["outstanding_balance"], row["total_transaction_amount"]), (0.0, 0.0))
        self.assertEqual(row["payment_status"], "Awaiting quotation")
        self.customer_post(quoted["reference"], "quotation/decline", {"quotation_revision": 1})
        self.assertEqual(customer_row()["outstanding_balance"], 0.0)
        # Once agreed, the totals are owed.
        self.admin_post(package["reference"], "confirm")
        revised = self.admin_post(quoted["reference"], "quotation", {"total_amount": 900})
        self.accept(revised, "down_payment")
        row = customer_row()
        self.assertEqual((row["outstanding_balance"], row["total_transaction_amount"]), (1200.0, 1200.0))
        self.assertEqual(row["payment_status"], "With balance")

    def test_legacy_orders_keep_their_payment_option_and_default_to_pickup(self) -> None:
        legacy = {
            "reference": "",
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "customer": {"name": "Old", "email": self.email, "phone": "", "messenger": ""},
            "product": {"id": "white-mug", "name": "White Mug", "category": "Souvenir", "description": "", "image": ""},
            "customization": {"quantity": 1, "notes": ""},
            "event_details": {},
            "has_event": False,
            "status": "Awaiting Down Payment",
            "attachments": [],
            "history": [{"at": "2026-10-01T00:00:00+00:00", "text": "Customer submitted an order request."}],
        }
        reference = order_repository.create_order(legacy, f"legacy-{uuid.uuid4().hex}")
        with connection_scope() as connection:
            connection.execute("UPDATE orders SET total_amount = 400 WHERE reference = ?", (reference,))
        view = self.admin_view(reference)
        self.assertEqual(view["payment_option"], "down_payment")
        self.assertEqual(view["fulfillment"], {"method": "pickup", "label": "Store Pickup", "recorded": False, "address": [], "release_status": "Ready for Pickup"})
        self.assertTrue(view["actions"]["record_payment"])
        paid = self.admin_post(reference, "payments", {"amount": 200, "method": "Cash", "received": True})
        self.assertEqual(paid["status"], "Draft Preparation")


if __name__ == "__main__":
    unittest.main()
