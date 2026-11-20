"""Order, quotation, payment, and confirmation workflow (the confirmed business rules).

1. Every order requires a 50% down payment; all amounts are calculated by the server.
2. Package prices and inclusions are admin-editable, shown to customers, used for the order total, and
   saved as a snapshot with each order.
3. Customized orders are priced only by the administrator's quotation, with a full quotation history and
   a customer notification for every new or revised quotation.
4. Customers accept or decline the latest quotation; declined or outdated quotations cannot proceed.
5. Customers submit a GCash screenshot/receipt; it stays Pending Verification until the admin verifies it.
6. Verifying the 50% down payment confirms the order automatically and notifies the customer once.
7. Customers cannot change prices or amounts or verify payments; duplicates are refused; changes are audited.
"""
from __future__ import annotations

import base64
import json
import math
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
from backend.app.repositories import notification_repository, order_repository  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
ADMIN_EMAIL = "admin@example.test"
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
        self.admin.cookies.set(SESSION_COOKIE, create_session_token(ADMIN_EMAIL, "admin"))
        self.shopper, self.email = self.new_customer()

    def new_customer(self) -> tuple[TestClient, str]:
        shopper = TestClient(app)
        response = shopper.post(
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
        return shopper, response.json()["customer"]["email"]

    # ------------------------------------------------------------ helpers

    def order_payload(self, product_id: str = "white-mug", quantity: int = 2, options: dict | None = None, **extra) -> dict:
        customization = {"quantity": quantity, "options": options or MUG_OPTIONS, "notes": ""}
        if "package_id" in extra:
            customization["package_id"] = extra.pop("package_id")
        return {
            "customer": {"name": "Workflow Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": customization,
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": [],
            **extra,
        }

    def place(self, product_id: str = "white-mug", quantity: int = 2, options: dict | None = None, expect: int = 201, **extra) -> dict:
        payload = self.order_payload(product_id, quantity, options, **extra)
        response = self.shopper.post("/api/orders", json=payload, headers={"Idempotency-Key": idempotency_key()})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def place_package(self, quantity: int = 25, package_id: str = "birthday-set-a", **extra) -> dict:
        return self.place("birthday-invitation", quantity, BIRTHDAY_PACKAGE_OPTIONS, package_id=package_id, **extra)

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

    def customer_post(self, reference: str, action: str, body: dict | None = None, expect: int = 200, client: TestClient | None = None) -> dict:
        response = (client or self.shopper).post(f"/api/orders/{reference}/{action}", json={"email": self.email, **(body or {})})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def customized_order(self, amount: float = 1000, **extra) -> dict:
        order = self.place(**extra)
        reference = order["reference"]
        self.admin_post(reference, "status", {"status": "Under Review"})
        return self.admin_post(reference, "quotation", {"total_amount": amount, "payment_instructions": "GCash 0917 000 0000"})

    def accept(self, order: dict, expect: int = 200, **body) -> dict:
        return self.customer_post(order["reference"], "quotation/accept", {"quotation_revision": order["quotation"]["revision"], **body}, expect)

    def approved_package(self, **extra) -> dict:
        order = self.place_package(**extra)
        return self.admin_post(order["reference"], "confirm", {"payment_instructions": "GCash 0917 000 0000 (Print Works)"})

    def submit_payment(self, reference: str, payment_reference: str | None = None, proof: bool = True, expect: int = 200, **body) -> dict:
        body = {"reference": payment_reference or unique_reference(), **body}
        if proof:
            body["proof"] = {"name": "gcash.png", "dataUrl": PNG}
        return self.customer_post(reference, "payments", body, expect)

    def pending_submission(self, reference: str) -> dict:
        return next(item for item in self.admin_view(reference)["payment_submissions"] if item["status"] == "pending")

    def verify(self, reference: str, expect: int = 200, **body) -> dict:
        submission = self.pending_submission(reference)
        return self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {"received": True, **body}, expect)

    def confirmed_order(self, amount: float = 1000, **extra) -> dict:
        reference = self.accept(self.customized_order(amount, **extra))["reference"]
        self.submit_payment(reference)
        return self.verify(reference)

    def through_draft_approval(self, reference: str) -> dict:
        self.admin_post(reference, "draft", {"file": {"name": "draft.png", "dataUrl": PNG}})
        return self.customer_post(reference, "approve")

    def notifications(self, client: TestClient | None = None, reference: str | None = None) -> list[dict]:
        response = (client or self.shopper).get("/api/account/notifications")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["notifications"]
        return [item for item in items if reference is None or item["order_reference"] == reference]

    def kinds(self, reference: str) -> list[str]:
        return sorted(item["kind"] for item in self.notifications(reference=reference))

    def audit(self, reference: str) -> list[dict]:
        response = self.admin.get(f"/api/admin/orders/{reference}/audit")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["audit"]


# ====================================================================== 1. down payment


class DownPaymentTests(WorkflowTestCase):
    def test_every_order_requires_the_50_percent_down_payment(self) -> None:
        package = self.place_package(25)
        # ₱300 for 20 pcs + 5 × ₱15 add-on.
        self.assertEqual(package["payment_option"], "down_payment")
        self.assertEqual(package["payment"]["down_payment_rate"], 0.5)
        self.assertEqual(package["payment"]["down_payment_required"], 187.5)
        self.assertEqual(package["payment"]["remaining_after_down_payment"], 187.5)

        quoted = self.customized_order(1000)
        self.assertEqual(quoted["payment_option"], "down_payment")
        self.assertEqual(quoted["payment"]["down_payment_required"], 500.0)
        self.assertEqual(quoted["payment"]["remaining_after_down_payment"], 500.0)
        self.assertEqual(quoted["quotation"]["down_payment"], 500.0)

    def test_cash_on_delivery_cannot_be_chosen(self) -> None:
        self.place_package(20, payment_option="cod", expect=422)
        self.place(payment_option="cod", expect=422)
        self.place(payment_option="credit", expect=422)
        quoted = self.customized_order(800)
        self.accept(quoted, expect=422, payment_option="cod")
        self.admin_post(quoted["reference"], "quotation", {"total_amount": 800, "cod_allowed": True}, expect=422)
        accepted = self.accept(quoted)
        self.assertEqual(accepted["status"], "Awaiting Down Payment")
        self.admin_post(accepted["reference"], "payment-option", {"payment_option": "cod"}, expect=422)

    def test_amounts_due_are_calculated_by_the_server(self) -> None:
        reference = self.accept(self.customized_order(1001))["reference"]
        view = self.admin_view(reference)
        self.assertEqual(view["payment"]["next_payment"], {"stage": "down_payment", "label": "50% down payment", "amount": 500.5})
        # The customer cannot pick a different amount, even a smaller or larger one.
        self.submit_payment(reference, amount=100, expect=422)
        self.submit_payment(reference, amount=1001, expect=422)
        submitted = self.submit_payment(reference)
        self.assertEqual(submitted["payment_submissions"][0]["amount"], 500.5)
        self.assertEqual(submitted["payment_submissions"][0]["stage"], "down_payment")
        verified = self.verify(reference)
        self.assertEqual(verified["payment"]["balance"], 500.5)
        self.assertEqual(verified["payment"]["next_payment"]["stage"], "balance")
        balance = self.submit_payment(reference, amount=500.5)
        self.assertEqual(balance["payment_submissions"][-1]["stage"], "balance")


# ====================================================================== 2. package pricing


class PackagePricingTests(WorkflowTestCase):
    def create_package(self, price: float, inclusions: list[str]) -> dict:
        name = f"Rule Set {uuid.uuid4().hex[:6]}"
        created = self.admin.post("/api/admin/catalog/products/birthday-invitation/packages", json={
            "name": name, "price": price, "inclusions": inclusions, "included_quantity": 20,
        })
        self.assertEqual(created.status_code, 201, created.text)
        return next(item for item in created.json()["packages"] if item["name"] == name)

    def test_package_edits_show_immediately_and_never_change_existing_orders(self) -> None:
        package = self.create_package(500, ["20 pcs invitations", "White envelopes"])
        try:
            public = self.client.get("/api/catalog/products/birthday-invitation/packages").json()["packages"]
            shown = next(item for item in public if item["id"] == package["id"])
            self.assertEqual((shown["price"], shown["inclusions"]), (500.0, ["20 pcs invitations", "White envelopes"]))

            first = self.place_package(20, package["id"])
            self.assertTrue(first["fixed_price"])
            self.assertEqual(first["total_amount"], 500.0)
            self.assertEqual(first["package"]["price"], 500.0)
            self.assertEqual(first["package"]["inclusions"], ["20 pcs invitations", "White envelopes"])

            edited = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={
                "price": 650, "inclusions": ["20 pcs invitations", "Gold envelopes", "Thank-you tags"],
            })
            self.assertEqual(edited.status_code, 200, edited.text)
            public = self.client.get("/api/catalog/products/birthday-invitation/packages").json()["packages"]
            shown = next(item for item in public if item["id"] == package["id"])
            self.assertEqual((shown["price"], len(shown["inclusions"])), (650.0, 3), "Edits reach the customer site at once.")

            # The earlier order keeps the price and inclusions it was placed with.
            before = self.admin_view(first["reference"])
            self.assertEqual(before["total_amount"], 500.0)
            self.assertEqual(before["package"]["price"], 500.0)
            self.assertEqual(before["package"]["inclusions"], ["20 pcs invitations", "White envelopes"])
            self.assertEqual(before["payment"]["down_payment_required"], 250.0)
            # A new order uses the new price.
            second = self.place_package(20, package["id"])
            self.assertEqual((second["total_amount"], second["payment"]["down_payment_required"]), (650.0, 325.0))
        finally:
            self.admin.delete(f"/api/admin/catalog/packages/{package['id']}")

    def test_package_order_is_approved_then_confirmed_by_the_verified_down_payment(self) -> None:
        order = self.place_package(25)
        reference = order["reference"]
        self.assertEqual(order["status"], "Pending Confirmation")
        # Nothing can be paid before the shop approves the order, and a package is never re-quoted.
        self.submit_payment(reference, expect=409)
        self.admin_post(reference, "payments", {"amount": 187.5, "method": "GCash", "received": True}, expect=409)
        self.admin_post(reference, "quotation", {"total_amount": 999}, expect=409)
        view = self.admin_view(reference)
        self.assertTrue(view["actions"]["confirm_package"])
        self.assertFalse(view["actions"]["quote"])

        approved = self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000"})
        self.assertEqual(approved["status"], "Awaiting Down Payment", "Approval requests the down payment; it is not the confirmation.")
        self.assertEqual(self.kinds(reference), ["down_payment_requested"])
        self.submit_payment(reference)
        self.assertEqual(self.admin_view(reference)["status"], "Awaiting Down Payment")
        verified = self.verify(reference)
        self.assertEqual(verified["status"], "Confirmed")
        self.assertEqual(verified["payment"]["down_payment_status"], "Paid")
        self.assertEqual(verified["payment"]["balance"], 187.5)
        self.assertEqual(self.kinds(reference), ["down_payment_requested", "order_confirmed"])


# ====================================================================== 3-4. quotations


class CustomizedQuotationTests(WorkflowTestCase):
    def test_customized_order_has_no_price_until_the_admin_quotes_it(self) -> None:
        order = self.place()
        reference = order["reference"]
        self.assertFalse(order["fixed_price"])
        self.assertIsNone(order["total_amount"])
        self.assertIsNone(order["payment"]["down_payment_required"], "No down payment is calculated from an estimate.")
        self.submit_payment(reference, expect=409)
        self.admin_post(reference, "status", {"status": "Under Review"})
        self.admin_post(reference, "payments", {"amount": 100, "method": "Cash", "received": True}, expect=409)
        self.admin_post(reference, "confirm", expect=409)
        quoted = self.admin_post(reference, "quotation", {"total_amount": 1200, "note": "Includes layout"})
        self.assertEqual((quoted["status"], quoted["total_amount"]), ("Quotation Sent", 1200.0))
        # Sent but not accepted: still nothing can be paid.
        self.submit_payment(reference, expect=409)
        self.admin_post(reference, "payments", {"amount": 600, "method": "Cash", "received": True}, expect=409)

    def test_quotation_history_and_notifications(self) -> None:
        order = self.place()
        reference = order["reference"]
        self.admin_post(reference, "status", {"status": "Under Review"})
        self.admin_post(reference, "quotation", {"total_amount": 1200, "note": "Includes layout"})
        self.assertEqual(self.kinds(reference), ["quotation_sent"])
        revised = self.admin_post(reference, "quotation", {"total_amount": 1000, "note": "Discount applied"})
        self.assertEqual(revised["quotation"]["revision"], 2)
        self.assertEqual(revised["quotation"]["previous_amount"], 1200.0)
        history = revised["quotation_history"]
        self.assertEqual([(item["revision"], item["amount"], item["previous_amount"], item["status"]) for item in history],
                         [(1, 1200.0, None, "superseded"), (2, 1000.0, 1200.0, "sent")])
        self.assertEqual([item["note"] for item in history], ["Includes layout", "Discount applied"])
        self.assertTrue(all(item["sent_at"] for item in history))
        self.assertTrue(history[0]["superseded_at"])
        self.assertEqual(history[1]["sent_by"], ADMIN_EMAIL)
        self.assertEqual(self.kinds(reference), ["quotation_revised", "quotation_sent"])
        revised_notice = next(item for item in self.notifications(reference=reference) if item["kind"] == "quotation_revised")
        self.assertIn("from PHP 1,200.00 to PHP 1,000.00", revised_notice["message"])

        # The customer sees the history too, without the administrator's identity.
        customer_view = self.shopper.get(f"/api/account/orders/{reference}").json()
        self.assertEqual(len(customer_view["quotation_history"]), 2)
        self.assertNotIn("sent_by", customer_view["quotation_history"][0])
        accepted = self.accept(customer_view)
        self.assertEqual([item["status"] for item in accepted["quotation_history"]], ["superseded", "accepted"])
        self.assertTrue(accepted["quotation_history"][1]["responded_at"])

    def test_customer_cannot_accept_an_outdated_quotation(self) -> None:
        first = self.customized_order(500)
        self.admin_post(first["reference"], "quotation", {"total_amount": 650})
        self.accept(first, expect=409)
        latest = self.shopper.get(f"/api/account/orders/{first['reference']}").json()
        accepted = self.accept(latest)
        self.assertEqual((accepted["total_amount"], accepted["status"]), (650.0, "Awaiting Down Payment"))
        self.assertEqual(accepted["payment"]["down_payment_required"], 325.0)

    def test_accepting_an_already_accepted_quotation_is_refused(self) -> None:
        quoted = self.customized_order(500)
        self.accept(quoted)
        self.accept(quoted, expect=409)

    def test_declined_quotation_cannot_be_paid_and_can_be_revised(self) -> None:
        quoted = self.customized_order(1500)
        reference = quoted["reference"]
        declined = self.customer_post(reference, "quotation/decline", {"quotation_revision": 1, "reason": "Over budget"})
        self.assertEqual(declined["status"], "Quotation Declined")
        self.assertEqual(declined["quotation_history"][0]["status"], "declined")
        self.assertEqual(declined["quotation_history"][0]["decline_reason"], "Over budget")
        self.assertIsNone(declined["payment"]["next_payment"])
        self.submit_payment(reference, expect=409)
        self.admin_post(reference, "payments", {"amount": 750, "method": "Cash", "received": True}, expect=409)
        self.accept(declined, expect=409)
        revised = self.admin_post(reference, "quotation", {"total_amount": 1300})
        self.assertEqual([item["status"] for item in revised["quotation_history"]], ["declined", "sent"])
        accepted = self.accept(revised)
        self.assertEqual((accepted["status"], accepted["total_amount"]), ("Awaiting Down Payment", 1300.0))

    def test_quotation_is_locked_once_money_is_reported(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference)
        self.admin_post(reference, "quotation", {"total_amount": 900}, expect=409)
        self.assertFalse(self.admin_view(reference)["actions"]["quote"])


# ====================================================================== 5-6. payment verification and confirmation


class PaymentVerificationTests(WorkflowTestCase):
    def test_gcash_receipt_is_required_and_waits_for_verification(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference, proof=False, expect=422)
        self.submit_payment(reference, method="Maya", expect=422)
        self.customer_post(reference, "payments", {"proof": {"name": "gcash.png", "dataUrl": PNG}}, expect=422)  # no reference number
        submitted = self.submit_payment(reference)
        item = submitted["payment_submissions"][0]
        self.assertEqual((item["status"], item["status_label"], item["method"]), ("pending", "Pending Verification", "GCash"))
        self.assertEqual(submitted["payment"]["down_payment_status"], "Pending Verification")
        self.assertEqual(submitted["payment"]["amount_paid"], 0.0)
        self.assertEqual(submitted["payment"]["pending_verification"], 500.0)
        # An uploaded screenshot never confirms the order by itself.
        self.assertEqual(submitted["status"], "Awaiting Down Payment")
        self.assertNotIn("order_confirmed", self.kinds(reference))
        self.assertEqual(self.shopper.get(item["proof"]["url"]).status_code, 200)
        view = self.admin_view(reference)
        self.assertTrue(view["actions"]["verify_payments"])
        self.assertFalse(view["actions"]["record_payment"])
        self.assertEqual(self.admin.get(view["payment_submissions"][0]["proof"]["url"]).status_code, 200)

    def test_verified_down_payment_confirms_the_order_and_notifies_once(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference)
        submission = self.pending_submission(reference)
        version = self.admin_view(reference)["version"]
        verified = self.verify(reference)
        self.assertEqual(verified["status"], "Confirmed")
        self.assertEqual(verified["payments"][0]["kind"], "Down payment")
        self.assertEqual(verified["payment_submissions"][0]["status"], "verified")
        self.assertIn("now Confirmed", verified["history"][-1]["text"])
        # The order is an active order now.
        active = self.admin.get("/api/admin/orders", params={"search": reference, "status": "processing"}).json()
        self.assertIn(reference, [row["reference"] for row in active["items"]])
        self.assertEqual(self.admin.get("/api/admin/counts").json()["confirmed"] >= 1, True)

        # Verifying again, or replaying the same request with the old version, changes nothing.
        path = f"/api/admin/orders/{reference}/payment-submissions/{submission['id']}/verify"
        self.assertEqual(self.admin.post(path, json={"received": True, "expected_version": version}).status_code, 409)
        self.assertEqual(self.admin.post(path, json={"received": True}).status_code, 409)
        stored = stored_order(reference)
        self.assertEqual(len(stored["payments"]), 1)
        confirmations = [item for item in self.notifications(reference=reference) if item["kind"] == "order_confirmed"]
        self.assertEqual(len(confirmations), 1)
        self.assertIn("down payment", confirmations[0]["message"])
        self.assertIn("confirmed", confirmations[0]["message"])

    def test_the_same_notification_is_never_stored_twice(self) -> None:
        reference = self.confirmed_order()["reference"]
        with connection_scope() as connection:
            again = notification_repository.insert(connection, reference, "2026-11-18T00:00:00+00:00", {
                "dedupe_key": f"{reference}:order-confirmed", "customer_id": None, "kind": "order_confirmed",
                "title": "duplicate", "message": "duplicate",
            })
        self.assertFalse(again)
        self.assertEqual([item["kind"] for item in self.notifications(reference=reference)].count("order_confirmed"), 1)

    def test_partial_verification_does_not_confirm(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference)
        partial = self.verify(reference, amount=400)
        self.assertEqual(partial["status"], "Awaiting Down Payment")
        self.assertIn("customer reported PHP 500.00", partial["history"][-1]["text"])
        self.assertEqual(partial["payment"]["next_payment"], {"stage": "down_payment", "label": "50% down payment", "amount": 100.0})
        self.assertEqual(self.kinds(reference), ["payment_verified", "quotation_sent"])
        self.submit_payment(reference)
        self.assertEqual(self.verify(reference)["status"], "Confirmed")
        self.assertIn("order_confirmed", self.kinds(reference))

    def test_admin_recorded_down_payment_also_confirms(self) -> None:
        reference = self.accept(self.customized_order(800))["reference"]
        paid = self.admin_post(reference, "payments", {"amount": 400, "method": "Cash", "received": True})
        self.assertEqual(paid["status"], "Confirmed")
        self.assertIn("order_confirmed", self.kinds(reference))

    def test_duplicate_submissions_and_references_are_refused(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        gcash = unique_reference()
        self.submit_payment(reference, gcash)
        # One submission waits at a time; the admin cannot record the same money separately meanwhile.
        self.submit_payment(reference, expect=409)
        self.admin_post(reference, "payments", {"amount": 500, "method": "GCash", "received": True}, expect=409)
        self.verify(reference)
        # The same GCash reference cannot be reused on this or any other order.
        self.submit_payment(reference, gcash.lower(), expect=409)
        other = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(other, gcash, expect=409)
        self.admin_post(other, "payments", {"amount": 500, "method": "GCash", "reference": gcash, "received": True}, expect=409)

    def test_rejected_submission_notifies_and_can_be_resubmitted(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        gcash = unique_reference()
        self.submit_payment(reference, gcash)
        submission = self.pending_submission(reference)
        self.admin_post(reference, f"payment-submissions/{submission['id']}/reject", {"note": ""}, expect=422)
        rejected = self.admin_post(reference, f"payment-submissions/{submission['id']}/reject", {"note": "Not found in GCash"})
        self.assertEqual(rejected["payment_submissions"][0]["status_label"], "Rejected")
        self.assertEqual((rejected["status"], rejected["payment"]["pending_verification"]), ("Awaiting Down Payment", 0.0))
        self.assertIn("payment_rejected", self.kinds(reference))
        self.admin_post(reference, f"payment-submissions/{submission['id']}/verify", {"received": True}, expect=409)
        self.submit_payment(reference, gcash)

    def test_non_finite_amounts_are_rejected(self) -> None:
        # Python's JSON parser accepts NaN/Infinity; they must never become a payment amount.
        reference = self.accept(self.customized_order(1000))["reference"]
        headers = {"Content-Type": "application/json"}
        for literal in ("NaN", "Infinity", "-Infinity"):
            submitted = self.shopper.post(
                f"/api/orders/{reference}/payments",
                content=f'{{"email": "{self.email}", "amount": {literal}, "reference": "{unique_reference()}", "proof": {{"name": "g.png", "dataUrl": "{PNG}"}}}}',
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
            quoted = self.admin.post(
                f"/api/admin/orders/{reference}/quotation",
                content=f'{{"total_amount": {literal}, "expected_version": {version}}}',
                headers=headers,
            )
            self.assertIn(quoted.status_code, (409, 422), f"quotation {literal}: {quoted.text}")
        self.submit_payment(reference)
        submission = self.pending_submission(reference)
        verified = self.admin.post(
            f"/api/admin/orders/{reference}/payment-submissions/{submission['id']}/verify",
            content=f'{{"amount": NaN, "received": true, "expected_version": {self.admin_view(reference)["version"]}}}',
            headers=headers,
        )
        self.assertEqual(verified.status_code, 422, verified.text)
        self.assertEqual(stored_order(reference)["payments"], [])
        self.assertTrue(math.isfinite(self.admin_view(reference)["total_amount"]))

    def test_customer_cannot_cancel_with_a_pending_payment(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference)
        self.customer_post(reference, "cancel", expect=409)
        self.admin_post(reference, "status", {"status": "Cancelled"}, expect=409)


# ====================================================================== 7. order security and audit


class OrderSecurityTests(WorkflowTestCase):
    def test_order_actions_need_the_signed_in_owner(self) -> None:
        quoted = self.customized_order(1000)
        reference = quoted["reference"]
        body = {"email": self.email, "quotation_revision": 1}
        # Knowing the email address and the reference is not enough to look an order up or to change it.
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get(f"/api/orders/{reference}", params={"email": self.email}).status_code, 401)
        self.assertEqual(anonymous.get("/api/orders", params={"email": self.email, "reference": reference}).status_code, 401)
        self.assertEqual(anonymous.post(f"/api/orders/{reference}/quotation/accept", json=body).status_code, 401)
        self.assertEqual(anonymous.post(f"/api/orders/{reference}/cancel", json=body).status_code, 401)
        stranger, _ = self.new_customer()
        self.assertEqual(stranger.get(f"/api/orders/{reference}", params={"email": self.email}).status_code, 404)
        for action in ("quotation/accept", "quotation/decline", "cancel", "payments", "approve"):
            self.assertEqual(stranger.post(f"/api/orders/{reference}/{action}", json=body).status_code, 404, action)
        self.assertEqual(self.admin_view(reference)["status"], "Quotation Sent")
        self.assertEqual(stranger.get("/api/account/notifications").json()["notifications"], [])

    def test_customers_cannot_change_prices_or_verify_payments(self) -> None:
        quoted = self.customized_order(1000)
        reference = quoted["reference"]
        accepted = self.accept(quoted, total_amount=1, down_payment_required=1)
        self.assertEqual((accepted["total_amount"], accepted["payment"]["down_payment_required"]), (1000.0, 500.0))
        self.submit_payment(reference, amount=1, expect=422)
        self.submit_payment(reference)
        submission = self.pending_submission(reference)
        for path, body in (
            (f"/api/admin/orders/{reference}/payment-submissions/{submission['id']}/verify", {"received": True}),
            (f"/api/admin/orders/{reference}/payments", {"amount": 500, "method": "Cash", "received": True}),
            (f"/api/admin/orders/{reference}/quotation", {"total_amount": 1}),
            (f"/api/admin/orders/{reference}/status", {"status": "Confirmed"}),
        ):
            self.assertEqual(self.shopper.post(path, json=body).status_code, 401, path)
        self.assertEqual(self.shopper.get(f"/api/admin/orders/{reference}/audit").status_code, 401)
        stored = stored_order(reference)
        self.assertEqual((stored["status"], stored["payments"], stored["total_amount"]), ("Awaiting Down Payment", [], 1000.0))

    def test_confirmed_cannot_be_set_by_hand(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.admin_post(reference, "status", {"status": "Confirmed"}, expect=409)
        package = self.approved_package()
        self.admin_post(package["reference"], "status", {"status": "Confirmed"}, expect=409)

    def test_audit_history_records_who_changed_what(self) -> None:
        order = self.place()
        reference = order["reference"]
        self.admin_post(reference, "status", {"status": "Under Review"})
        self.admin_post(reference, "quotation", {"total_amount": 1200})
        revised = self.admin_post(reference, "quotation", {"total_amount": 1100, "note": "Revised"})
        self.accept(revised)
        self.submit_payment(reference)
        self.verify(reference)
        entries = self.audit(reference)
        self.assertEqual([entry["action"] for entry in entries], [
            "order.created", "order.status_changed", "quotation.sent", "quotation.revised",
            "quotation.accepted", "payment.submitted", "payment.verified",
        ])
        by_action = {entry["action"]: entry for entry in entries}
        customer_id = self.shopper.get("/api/customer/session").json()["customer"]["id"]
        self.assertEqual((by_action["order.created"]["actor_type"], by_action["order.created"]["actor_id"]), ("customer", customer_id))
        self.assertEqual((by_action["quotation.revised"]["actor_type"], by_action["quotation.revised"]["actor_id"]), ("admin", ADMIN_EMAIL))
        self.assertEqual((by_action["quotation.revised"]["details"]["previous_amount"], by_action["quotation.revised"]["details"]["amount"]), (1200.0, 1100.0))
        self.assertEqual((by_action["quotation.revised"]["details"]["total_from"], by_action["quotation.revised"]["details"]["total_to"]), (1200.0, 1100.0))
        self.assertEqual(by_action["payment.verified"]["details"]["status_to"], "Confirmed")
        self.assertTrue(by_action["payment.verified"]["details"]["confirmed"])
        self.assertTrue(all(entry["at"] for entry in entries))
        # A refused action leaves no audit entry.
        self.admin_post(reference, "quotation", {"total_amount": 900}, expect=409)
        self.assertEqual(len(self.audit(reference)), len(entries))
        sent = self.admin.get(f"/api/admin/orders/{reference}/audit").json()["notifications"]
        self.assertEqual(sorted(item["kind"] for item in sent), ["order_confirmed", "quotation_revised", "quotation_sent"])

    def test_notifications_can_be_marked_read(self) -> None:
        reference = self.customized_order(1000)["reference"]
        listing = self.shopper.get("/api/account/notifications").json()
        self.assertEqual(listing["unread"], 1)
        marked = self.shopper.post("/api/account/notifications/read", json={"ids": [listing["notifications"][0]["id"]]}).json()
        self.assertEqual(marked["unread"], 0)
        self.assertTrue(marked["notifications"][0]["read"])
        self.assertEqual(self.shopper.post("/api/account/notifications/read", json={"ids": "all"}).status_code, 422)
        self.assertEqual(TestClient(app).get("/api/account/notifications").status_code, 401)
        self.assertEqual(self.notifications(reference=reference)[0]["kind"], "quotation_sent")


# ====================================================================== production, fulfillment, older records


class ProductionAndFulfillmentTests(WorkflowTestCase):
    def test_delivery_details_are_required_and_stored(self) -> None:
        incomplete = {**DELIVERY, "barangay": ""}
        for fulfillment in (incomplete, {"method": "drone"}, {**DELIVERY, "contact_number": "abc"}):
            response = self.shopper.post(
                "/api/orders", json=self.order_payload(fulfillment=fulfillment), headers={"Idempotency-Key": idempotency_key()}
            )
            self.assertEqual(response.status_code, 422, response.text)
        order = self.place(fulfillment=DELIVERY)
        self.assertEqual(order["fulfillment"]["method"], "delivery")
        address = {row["key"]: row["value"] for row in order["fulfillment"]["address"]}
        self.assertEqual(address["barangay"], "San Roque")
        self.assertEqual(address["landmark"], "Across the chapel")
        self.assertEqual(order["fulfillment"]["release_status"], "Out for Delivery")
        pickup = self.place(fulfillment={"method": "pickup", "street": "ignored"})
        self.assertEqual((pickup["fulfillment"]["method"], pickup["fulfillment"]["address"]), ("pickup", []))
        self.assertEqual(stored_order(pickup["reference"]).get("fulfillment"), {"method": "pickup"})

    def test_full_delivery_flow_from_confirmation_to_completion(self) -> None:
        reference = self.confirmed_order(1000, fulfillment=DELIVERY)["reference"]
        # No production before the customer approves a draft.
        self.admin_post(reference, "status", {"status": "In Production"}, expect=409)
        self.assertEqual(self.admin_view(reference)["allowed_statuses"], ["Draft Preparation"])
        self.assertTrue(self.admin_view(reference)["actions"]["send_draft"])
        self.admin_post(reference, "draft", {"file": {"name": "draft-1.png", "dataUrl": PNG}})
        self.assertIn("draft_ready", self.kinds(reference))
        revision = self.customer_post(reference, "request-changes", {"note": "Bigger name please"})
        self.assertEqual(revision["status"], "Draft Preparation")
        approved = self.through_draft_approval(reference)
        self.assertEqual(approved["status"], "Approved for Printing")
        self.assertIn("revised draft", approved["history"][-2]["text"])
        self.admin_post(reference, "status", {"status": "In Production"})
        self.assertEqual(self.admin_view(reference)["allowed_statuses"], ["Out for Delivery"])
        self.admin_post(reference, "status", {"status": "Ready for Pickup"}, expect=409)
        self.admin_post(reference, "status", {"status": "Out for Delivery"})
        # Not completed while a balance is outstanding; the customer pays the remaining 50% through GCash.
        self.assertTrue(self.admin_view(reference)["actions"]["complete_requires_payment"])
        self.admin_post(reference, "status", {"status": "Completed"}, expect=409)
        self.submit_payment(reference)
        collected = self.verify(reference)
        self.assertEqual(collected["payments"][-1]["kind"], "Balance payment")
        completed = self.admin_post(reference, "status", {"status": "Completed"})
        self.assertEqual((completed["status"], completed["payment"]["balance"]), ("Completed", 0.0))
        self.assertIn("status_changed", self.kinds(reference))

    def test_production_needs_the_down_payment_even_for_older_records(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        # Simulate an inconsistent older record: approved for printing without the verified down payment.
        with connection_scope() as connection:
            connection.execute(
                "UPDATE orders SET status = 'Approved for Printing', draft_attachment = ? WHERE reference = ?",
                (json.dumps({"id": "d1", "name": "d.png", "filename": "x.png", "mime_type": "image/png", "size": 1}), reference),
            )
        self.assertTrue(self.admin_view(reference)["actions"]["start_production_blocked"])
        self.admin_post(reference, "status", {"status": "In Production"}, expect=409)

    def legacy_order(self, status: str, total: float | None, payment_option: str = "down_payment", **customization) -> str:
        legacy = {
            "reference": "",
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
            "customer": {"name": "Old", "email": self.email, "phone": "", "messenger": ""},
            "product": {"id": "white-mug", "name": "White Mug", "category": "Souvenir", "description": "", "image": ""},
            "customization": {"quantity": 1, "notes": "", **customization},
            "event_details": {},
            "has_event": False,
            "status": status,
            "attachments": [],
            "history": [{"at": "2026-10-01T00:00:00+00:00", "text": "Customer submitted an order request."}],
            "payment_option": payment_option,
        }
        reference = order_repository.create_order(legacy, f"legacy-{uuid.uuid4().hex}")
        with connection_scope() as connection:
            connection.execute("UPDATE orders SET total_amount = ? WHERE reference = ?", (total, reference))
        return reference

    def test_legacy_orders_default_to_pickup_and_confirm_on_payment(self) -> None:
        reference = self.legacy_order("Awaiting Down Payment", 400)
        view = self.admin_view(reference)
        self.assertEqual(view["payment_option"], "down_payment")
        self.assertEqual(view["fulfillment"], {"method": "pickup", "label": "Store Pickup", "recorded": False, "address": [], "release_status": "Ready for Pickup"})
        self.assertTrue(view["actions"]["record_payment"])
        # Placed before accounts existed: the signed-in customer with the same email may still act on it.
        self.assertEqual(self.shopper.get(f"/api/orders/{reference}", params={"email": self.email}).status_code, 200)
        paid = self.admin_post(reference, "payments", {"amount": 200, "method": "Cash", "received": True})
        self.assertEqual(paid["status"], "Confirmed")

    def test_older_cash_on_delivery_orders_switch_to_the_down_payment(self) -> None:
        reference = self.legacy_order("Awaiting COD Confirmation", 600, "cod")
        view = self.admin_view(reference)
        self.assertFalse(view["actions"]["confirm_cod"])
        self.assertTrue(view["actions"]["change_payment_option"])
        self.assertEqual(self.admin.post(f"/api/admin/orders/{reference}/confirm-cod", json={}).status_code, 404)
        switched = self.admin_post(reference, "payment-option", {"payment_option": "down_payment", "note": "COD discontinued"})
        self.assertEqual((switched["payment_option"], switched["status"]), ("down_payment", "Awaiting Down Payment"))
        self.assertEqual(switched["payment"]["down_payment_required"], 300.0)
        package = self.legacy_order("Pending Confirmation", 300, "cod", fixed_price=True, order_kind="package")
        approved = self.admin_post(package, "confirm")
        self.assertEqual((approved["payment_option"], approved["status"]), ("down_payment", "Awaiting Down Payment"))


class AdminOverviewTests(WorkflowTestCase):
    def test_badges_and_payment_page_show_work_waiting_for_the_admin(self) -> None:
        before = self.admin.get("/api/admin/counts").json()
        reference = self.accept(self.customized_order(1000))["reference"]
        self.submit_payment(reference)
        after = self.admin.get("/api/admin/counts").json()
        self.assertEqual(after["payments_to_verify"], before["payments_to_verify"] + 1)
        payments = self.admin.get("/api/admin/payments").json()
        self.assertIn(reference, [item["reference"] for item in payments["summary"]["payments_to_verify"]])
        listing = self.admin.get("/api/admin/orders", params={"search": reference}).json()["items"][0]
        self.assertEqual(listing["payments_to_verify"], 1)
        self.assertEqual(listing["fulfillment_label"], "Store Pickup")
        self.verify(reference)
        activity = self.admin.get("/api/admin/activity", params={"limit": 80}).json()["events"]
        titles = {event["title"] for event in activity if event["reference"] == reference}
        self.assertTrue({"Quotation sent", "Quotation accepted", "Payment submitted for verification", "Down payment verified · order confirmed"} <= titles)

    def test_unagreed_prices_are_not_counted_as_owed(self) -> None:
        def customer_row() -> dict:
            return self.admin.get("/api/admin/customers", params={"search": self.email}).json()["items"][0]

        quoted = self.customized_order(1000)
        package = self.place_package(20)
        # A quotation the customer has not accepted and a package the shop has not approved are offers, not debts.
        row = customer_row()
        self.assertEqual((row["outstanding_balance"], row["total_transaction_amount"]), (0.0, 0.0))
        self.assertEqual(row["payment_status"], "Awaiting quotation")
        self.customer_post(quoted["reference"], "quotation/decline", {"quotation_revision": 1})
        self.assertEqual(customer_row()["outstanding_balance"], 0.0)
        # Once agreed, the totals are owed.
        self.admin_post(package["reference"], "confirm")
        revised = self.admin_post(quoted["reference"], "quotation", {"total_amount": 900})
        self.accept(revised)
        row = customer_row()
        self.assertEqual((row["outstanding_balance"], row["total_transaction_amount"]), (1200.0, 1200.0))
        self.assertEqual(row["payment_status"], "With balance")



# ====================================================================== 8. order access, files, and repeated submissions


class OrderAccessTests(WorkflowTestCase):
    """Orders, their files, and repeated submissions are only ever served to the signed-in account that owns them."""

    def place_with_file(self, client: TestClient, email: str, key: str | None = None) -> tuple[dict, int, str]:
        payload = self.order_payload(files=[{"name": "artwork.png", "dataUrl": PNG}])
        payload["customer"]["email"] = email
        key = key or idempotency_key()
        response = client.post("/api/orders", json=payload, headers={"Idempotency-Key": key})
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json(), response.status_code, key

    def test_guest_reusing_an_idempotency_key_gets_nothing(self) -> None:
        order, _, key = self.place_with_file(self.shopper, self.email)
        payload = self.order_payload()
        response = TestClient(app).post("/api/orders", json=payload, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(order["reference"], response.text)
        self.assertNotIn(self.email, response.text)

    def test_another_customer_reusing_an_idempotency_key_gets_a_conflict_without_the_order(self) -> None:
        order, _, key = self.place_with_file(self.shopper, self.email)
        stranger, stranger_email = self.new_customer()
        payload = self.order_payload()
        payload["customer"]["email"] = stranger_email
        response = stranger.post("/api/orders", json=payload, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(order["reference"], response.text)
        self.assertNotIn(self.email, response.text)

    def test_repeated_submission_by_the_owner_returns_the_same_order_once(self) -> None:
        first, first_status, key = self.place_with_file(self.shopper, self.email)
        again, again_status, _ = self.place_with_file(self.shopper, self.email, key)
        self.assertEqual((first_status, again_status), (201, 200))
        self.assertEqual(again["reference"], first["reference"])
        # A brand-new account placed exactly one order, however many times the request was sent.
        owner = stored_order(first["reference"]).get("customer_id") or ""
        self.assertEqual(len(order_repository.list_orders_for_customer(owner)), 1)

    def test_customer_cannot_read_another_customers_order(self) -> None:
        order, _, _ = self.place_with_file(self.shopper, self.email)
        reference = order["reference"]
        stranger, _ = self.new_customer()
        for path in (f"/api/orders/{reference}", f"/api/account/orders/{reference}"):
            response = stranger.get(path, params={"email": self.email})
            self.assertEqual(response.status_code, 404, path)
            self.assertNotIn(self.email, response.text)
        listed = stranger.get("/api/orders", params={"email": self.email, "reference": reference})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["orders"], [])
        for path in (f"/api/orders/{reference}", f"/api/account/orders/{reference}", "/api/orders", "/api/account/orders"):
            self.assertEqual(TestClient(app).get(path, params={"email": self.email, "reference": reference}).status_code, 401, path)

    def test_customer_cannot_download_another_customers_file(self) -> None:
        order, _, _ = self.place_with_file(self.shopper, self.email)
        file_url = order["attachments"][0]["url"]
        self.assertNotIn("email", file_url)
        stranger, _ = self.new_customer()
        self.assertEqual(stranger.get(file_url).status_code, 404)
        self.assertEqual(stranger.get(file_url, params={"email": self.email}).status_code, 404)
        self.assertEqual(TestClient(app).get(file_url, params={"email": self.email}).status_code, 401)

    def test_owner_reads_their_order_and_downloads_their_file(self) -> None:
        order, _, _ = self.place_with_file(self.shopper, self.email)
        reference = order["reference"]
        for path in (f"/api/orders/{reference}", f"/api/account/orders/{reference}"):
            response = self.shopper.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.json()["reference"], reference)
        self.assertIn(reference, [item["reference"] for item in self.shopper.get("/api/orders").json()["orders"]])
        download = self.shopper.get(order["attachments"][0]["url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, base64.b64decode(PNG.split(",", 1)[1]))
        self.assertIn("no-store", download.headers["cache-control"])
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")

    def test_admin_still_reads_files_and_manages_the_order(self) -> None:
        order, _, _ = self.place_with_file(self.shopper, self.email)
        reference = order["reference"]
        view = self.admin_view(reference)
        self.assertTrue(view["attachments"][0]["url"].startswith(f"/api/admin/orders/{reference}/files/"))
        self.assertEqual(self.admin.get(view["attachments"][0]["url"]).status_code, 200)
        updated = self.admin_post(reference, "status", {"status": "Under Review"})
        self.assertEqual(updated["status"], "Under Review")
        # The customer still sees the admin's change on their own order.
        self.assertEqual(self.shopper.get(f"/api/orders/{reference}").json()["status"], "Under Review")
        # An admin session is not a customer session, and a customer session is not an admin session.
        self.assertEqual(self.admin.get(f"/api/orders/{reference}").status_code, 401)
        self.assertEqual(self.shopper.get(view["attachments"][0]["url"]).status_code, 401)

    def test_existing_order_records_are_unchanged_by_reads_and_downloads(self) -> None:
        order, _, _ = self.place_with_file(self.shopper, self.email)
        reference = order["reference"]
        with connection_scope() as connection:
            before = dict(connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone())
        stranger, _ = self.new_customer()
        self.shopper.get(f"/api/orders/{reference}")
        self.shopper.get(order["attachments"][0]["url"])
        stranger.get(f"/api/orders/{reference}")
        stranger.get(order["attachments"][0]["url"])
        TestClient(app).get(f"/api/orders/{reference}", params={"email": self.email})
        with connection_scope() as connection:
            after = dict(connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone())
        self.assertEqual(after, before)

    def test_order_placed_before_accounts_opens_only_for_the_matching_signed_in_account(self) -> None:
        # The existing rule for orders without an owner (also used for quotation, payment, and cancel actions).
        reference = ProductionAndFulfillmentTests.legacy_order(self, "Awaiting Down Payment", 400)  # type: ignore[arg-type]
        self.assertEqual(self.shopper.get(f"/api/orders/{reference}").status_code, 200)
        stranger, _ = self.new_customer()
        self.assertEqual(stranger.get(f"/api/orders/{reference}", params={"email": self.email}).status_code, 404)
        self.assertEqual(TestClient(app).get(f"/api/orders/{reference}", params={"email": self.email}).status_code, 401)
        # Not listed by email: an account only lists the orders it placed.
        self.assertNotIn(reference, [item["reference"] for item in self.shopper.get("/api/orders").json()["orders"]])

if __name__ == "__main__":
    unittest.main()
