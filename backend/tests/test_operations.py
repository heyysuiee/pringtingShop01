"""E-wallet payment methods, delivery fees, draft history, production, quality checking, packaging, pickup,
courier delivery, cancellation requests, refunds, and the financial figures that depend on them.

Every rule here is enforced by the server: the tests call the HTTP API as the customer and the administrator.
"""
from __future__ import annotations

import base64
import copy
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-operations-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.services import report_services  # noqa: E402
from backend.tests import test_order_workflow as workflow  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


PNG = workflow.PNG
CHECKS_PASSED = {key: True for key in ("product_quality", "print_quality", "color_accuracy", "quantity_verified", "material_condition")}
CEBU = {**workflow.DELIVERY, "city": "Cebu City", "province": "Cebu", "barangay": "Lahug"}
RECIPIENT = {"recipient_name": "Workflow Customer", "recipient_number": "09171234567"}


def proof(name: str = "proof.png") -> list[dict]:
    return [{"name": name, "dataUrl": PNG}]


def tracking() -> str:
    return f"JT{uuid.uuid4().int % 10**12:012d}"


class OperationsTestCase(workflow.WorkflowTestCase):
    def to_packaging(self, reference: str) -> dict:
        self.through_draft_approval(reference)
        self.admin_post(reference, "status", {"status": "In Production"})
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        return self.admin_post(reference, "quality-check", {"checks": CHECKS_PASSED})

    def pay_balance(self, reference: str) -> dict:
        self.submit_payment(reference)
        return self.verify(reference)

    def settings(self) -> dict:
        response = self.admin.get("/api/admin/settings/fulfillment")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_settings(self, settings: dict, expect: int = 200) -> dict:
        response = self.admin.post("/api/admin/settings/fulfillment", json=settings)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()


# ====================================================================== payment methods


class PaymentMethodTests(OperationsTestCase):
    def methods(self) -> list[dict]:
        response = self.admin.get("/api/admin/settings/payment-methods")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["methods"]

    def method(self, provider: str) -> dict:
        return next(item for item in self.methods() if item["provider"] == provider)

    def test_gcash_and_maya_are_configured_and_only_active_methods_are_public(self) -> None:
        maya = self.method("Maya")
        self.assertIn("GCash", [item["provider"] for item in self.client.get("/api/payment-methods").json()["methods"]])
        try:
            updated = self.admin.patch(f"/api/admin/settings/payment-methods/{maya['id']}", json={
                "active": True, "account_name": "ImPRESS Print Works", "account_number": "0918 000 1111",
                "instructions": "Send the exact amount.", "qr": {"name": "maya-qr.png", "dataUrl": PNG},
            })
            self.assertEqual(updated.status_code, 200, updated.text)
            public = {item["provider"]: item for item in self.client.get("/api/payment-methods").json()["methods"]}
            self.assertEqual(public["Maya"]["account_number"], "0918 000 1111")
            qr = self.client.get(public["Maya"]["qr_url"])
            self.assertEqual((qr.status_code, qr.headers["content-type"]), (200, "image/png"))

            # A Maya payment is accepted and recorded with the Maya method.
            reference = self.accept(self.customized_order(800))["reference"]
            submitted = self.submit_payment(reference, method="maya")
            self.assertEqual(submitted["payment_submissions"][0]["method"], "Maya")
            verified = self.verify(reference)
            self.assertEqual((verified["status"], verified["payments"][0]["method"]), ("Confirmed", "Maya"))
        finally:
            self.admin.patch(f"/api/admin/settings/payment-methods/{maya['id']}", json={"active": False, "remove_qr": True})
        self.assertNotIn("Maya", [item["provider"] for item in self.client.get("/api/payment-methods").json()["methods"]])
        # An inactive or unknown method cannot be used by customers.
        reference = self.accept(self.customized_order(600))["reference"]
        self.submit_payment(reference, method="Maya", expect=422)
        self.submit_payment(reference, method="PayPal", expect=422)
        self.submit_payment(reference, payment_reference="12", expect=422)

    def test_administrators_add_other_e_wallets_and_customers_cannot_configure_them(self) -> None:
        name = f"ShopeePay {uuid.uuid4().hex[:4]}"
        self.assertIn(self.shopper.post("/api/admin/settings/payment-methods", json={"provider": name}).status_code, {401, 403})
        created = self.admin.post("/api/admin/settings/payment-methods", json={
            "provider": name, "account_name": "ImPRESS", "account_number": "0917 222 3333", "active": True, "display_order": 5,
        })
        self.assertEqual(created.status_code, 201, created.text)
        try:
            duplicate = self.admin.post("/api/admin/settings/payment-methods", json={"provider": name.upper()})
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            reference = self.accept(self.customized_order(500))["reference"]
            self.assertEqual(self.submit_payment(reference, method=name)["payment_submissions"][0]["method"], name)
            # The administrator can record payments made through it as well.
            self.assertIn(name, self.admin_view(reference)["payment_methods"])
        finally:
            self.admin.delete(f"/api/admin/settings/payment-methods/{created.json()['id']}")

    def test_the_last_active_method_cannot_be_turned_off(self) -> None:
        active = [item for item in self.methods() if item["active"]]
        for item in active[1:]:
            self.admin.patch(f"/api/admin/settings/payment-methods/{item['id']}", json={"active": False})
        try:
            refused = self.admin.patch(f"/api/admin/settings/payment-methods/{active[0]['id']}", json={"active": False})
            self.assertEqual(refused.status_code, 409, refused.text)
        finally:
            for item in active[1:]:
                self.admin.patch(f"/api/admin/settings/payment-methods/{item['id']}", json={"active": True})

    def test_active_payment_method_requires_account_details_and_preserves_history(self) -> None:
        name = f"TestWallet {uuid.uuid4().hex[:4]}"
        missing = self.admin.post("/api/admin/settings/payment-methods", json={
            "provider": name, "account_name": "", "account_number": "", "active": True,
        })
        self.assertEqual(missing.status_code, 422, missing.text)

        created = self.admin.post("/api/admin/settings/payment-methods", json={
            "provider": name, "account_name": "ImPRESS", "account_number": "0917 111 2222",
            "active": True, "display_order": 3, "instructions": "Send exact amount.",
        })
        self.assertEqual(created.status_code, 201, created.text)
        method_id = created.json()["id"]
        try:
            reference = self.accept(self.customized_order(700))["reference"]
            self.submit_payment(reference, method=name)
            verified = self.verify(reference)
            self.assertEqual(verified["payments"][0]["method"], name)

            renamed = self.admin.patch(f"/api/admin/settings/payment-methods/{method_id}", json={
                "provider": f"{name} Renamed", "active": False,
            })
            self.assertEqual(renamed.status_code, 200, renamed.text)

            detail = self.admin_view(reference)
            self.assertEqual(detail["payments"][0]["method"], name)
            self.assertEqual(detail["payment_submissions"][0]["method"], name)
            self.assertNotIn(f"{name} Renamed", [item["provider"] for item in self.client.get("/api/payment-methods").json()["methods"]])
        finally:
            self.admin.delete(f"/api/admin/settings/payment-methods/{method_id}")

    def test_proof_files_are_validated(self) -> None:
        reference = self.accept(self.customized_order(500))["reference"]
        fake = "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode()
        self.customer_post(reference, "payments", {"reference": workflow.unique_reference(), "proof": {"name": "x.png", "dataUrl": fake}}, expect=422)
        self.customer_post(reference, "payments", {"reference": workflow.unique_reference(), "proof": {"name": "x.exe", "dataUrl": PNG}}, expect=422)


# ====================================================================== delivery fees


class DeliveryFeeTests(OperationsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original = self.settings()

    def tearDown(self) -> None:
        self.save_settings(self.original)

    def set_fee(self, region: str, fee: float) -> None:
        settings = copy.deepcopy(self.original)
        for area in settings["areas"]:
            if area["region"] == region:
                area["fee"] = fee
        self.save_settings(settings)

    def test_delivery_fee_is_added_by_the_server_and_split_50_50(self) -> None:
        self.set_fee("visayas", 150)
        options = self.client.get("/api/fulfillment-options").json()
        self.assertEqual(next(item for item in options["areas"] if item["region"] == "visayas")["fee"], 150)
        quoted = self.customized_order(2000, fulfillment={**CEBU, "delivery_fee": 0})
        # The example from the payment policy: ₱2,000 + ₱150 delivery = ₱2,150; ₱1,075 down, ₱1,075 balance.
        self.assertEqual((quoted["total_amount"], quoted["quotation"]["delivery_fee"], quoted["quotation"]["total"]), (2150.0, 150.0, 2150.0))
        self.assertEqual(quoted["payment"]["subtotal"], 2000.0)
        accepted = self.accept(quoted)
        self.assertEqual((accepted["payment"]["down_payment_required"], accepted["payment"]["remaining_after_down_payment"]), (1075.0, 1075.0))
        self.assertIn("5–10 days", accepted["fulfillment"]["estimate_text"])
        # Fixed-price orders include the fee in their published total.
        package = self.place_package(25, fulfillment=CEBU)
        self.assertEqual((package["total_amount"], package["payment"]["delivery_fee"]), (375.0 + 150.0, 150.0))
        # Luzon uses the official default fee when the administrator has not overridden it.
        self.assertEqual(self.place(fulfillment=workflow.DELIVERY)["fulfillment"]["delivery_fee"], 58.0)
        # A cheaper area cannot be chosen for an address outside it.
        self.place(fulfillment={**CEBU, "area_id": "luzon"}, expect=422)
        self.assertEqual(self.place(fulfillment={**CEBU, "area_id": "visayas"})["fulfillment"]["delivery_fee"], 150.0)

    def test_delivery_can_be_turned_off_and_areas_deactivated(self) -> None:
        settings = copy.deepcopy(self.original)
        for area in settings["areas"]:
            area["active"] = area["region"] != "mindanao"
        self.save_settings(settings)
        self.place(fulfillment={**CEBU, "province": "Davao del Sur"}, expect=422)
        self.save_settings({**settings, "delivery_enabled": False})
        self.place(fulfillment=workflow.DELIVERY, expect=422)
        self.assertEqual(self.place(fulfillment={"method": "pickup"})["fulfillment"]["method"], "pickup")

    def test_invalid_settings_are_refused(self) -> None:
        bad_area = copy.deepcopy(self.original)
        bad_area["areas"][0]["fee"] = -5
        self.save_settings(bad_area, expect=422)
        bad_days = copy.deepcopy(self.original)
        bad_days["areas"][0].update({"estimate_min_days": 9, "estimate_max_days": 3})
        self.save_settings(bad_days, expect=422)
        bad_link = copy.deepcopy(self.original)
        bad_link["couriers"][0]["tracking_url"] = "http://example.test/track"
        self.save_settings(bad_link, expect=422)
        self.assertIn(self.shopper.post("/api/admin/settings/fulfillment", json=self.original).status_code, {401, 403})


# ====================================================================== drafts and production


class DraftAndProductionTests(OperationsTestCase):
    def test_draft_history_and_revision_requests_are_kept(self) -> None:
        reference = self.confirmed_order(900)["reference"]
        self.admin_post(reference, "draft", {"file": {"name": "first.png", "dataUrl": PNG}, "remarks": "Layout A"})
        revised = self.customer_post(reference, "request-changes", {"note": "Use a bigger font for the name"})
        self.assertEqual(revised["status"], "Draft Preparation")
        self.assertIn("revision_requested", self.kinds(reference))
        self.admin_post(reference, "draft", {"file": {"name": "second.png", "dataUrl": PNG}})
        approved = self.customer_post(reference, "approve")
        drafts = approved["drafts"]
        self.assertEqual([item["status"] for item in drafts], ["revision_requested", "approved"])
        self.assertEqual((drafts[0]["remarks"], drafts[0]["revision_note"]), ("Layout A", "Use a bigger font for the name"))
        # Both drafts can still be opened by the customer.
        self.assertEqual(self.shopper.get(drafts[0]["file"]["url"]).status_code, 200)
        self.assertIn("draft_approved", self.kinds(reference))

    def test_production_requires_payment_and_draft_and_records_progress(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.admin_post(reference, "production", {"progress": 10}, expect=409)
        self.through_draft_approval(reference)
        started = self.admin_post(reference, "status", {"status": "In Production"})
        self.assertTrue(started["production"]["started_at"])
        self.assertIn("status_changed", self.kinds(reference))
        self.admin_post(reference, "production", {"progress": 140}, expect=422)
        self.admin_post(reference, "production", {"printed_quantity": 1, "completed_quantity": 2}, expect=422)
        self.admin_post(reference, "production", {}, expect=422)
        updated = self.admin_post(reference, "production", {
            "estimated_completion": "2026-12-20", "personnel": "Press team B", "printed_quantity": 2, "progress": 60,
            "remarks": "Printing on matte stock", "photos": proof("press.png"),
        })
        production = updated["production"]
        self.assertEqual((production["progress"], production["personnel"], production["estimated_completion"]), (60, "Press team B", "2026-12-20"))
        self.assertEqual(len(production["photos"]), 1)
        self.assertEqual(len(production["updates"]), 1)

    def test_quality_check_failure_returns_to_production_and_blocks_packaging(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.through_draft_approval(reference)
        self.admin_post(reference, "status", {"status": "In Production"})
        self.admin_post(reference, "packaging", {"completed_quantity": 2}, expect=409)
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        failed_checks = {**CHECKS_PASSED, "color_accuracy": False}
        self.admin_post(reference, "quality-check", {"checks": failed_checks}, expect=422)
        self.admin_post(reference, "quality-check", {"checks": {"print_quality": True}}, expect=422)
        rework = self.admin_post(reference, "quality-check", {"checks": failed_checks, "defects": "Red is too dark", "corrective_actions": "Reprint with corrected profile"})
        self.assertEqual(rework["status"], "In Production")
        self.assertFalse(rework["production"]["quality_check"]["passed"])
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        passed = self.admin_post(reference, "quality-check", {"checks": CHECKS_PASSED, "remarks": "OK"})
        self.assertEqual(passed["status"], "Packaging")
        self.assertEqual(len(passed["production"]["quality_history"]), 1)
        self.assertIn("quality_checked", self.kinds(reference))
        self.admin_post(reference, "packaging", {"completed_quantity": 0}, expect=422)
        self.admin_post(reference, "packaging", {"completed_quantity": 99}, expect=422)


# ====================================================================== pickup


class PickupTests(OperationsTestCase):
    def test_fully_paid_pickup_skips_awaiting_payment_and_requires_a_verified_claimant(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.pay_balance(reference)
        self.to_packaging(reference)
        released = self.admin_post(reference, "packaging", {"completed_quantity": 2, "remarks": "Boxed", "proof": proof()})
        self.assertEqual(released["status"], "Ready for Pickup")
        self.assertNotIn("pickup_code", released["fulfillment"])
        self.assertTrue(released["fulfillment"]["pickup_code_set"])
        code = self.shopper.get(f"/api/account/orders/{reference}").json()["fulfillment"]["pickup_code"]
        self.assertIn("ready_for_pickup", self.kinds(reference))
        self.admin_post(reference, "status", {"status": "Completed"}, expect=409)
        base = {"claimant_name": "Ana Reyes", "claimant_type": "customer", "verification_code": code, "acknowledged": True}
        self.admin_post(reference, "pickup/confirm", {**base, "verification_code": "WRONG1"}, expect=422)
        self.admin_post(reference, "pickup/confirm", {**base, "acknowledged": False}, expect=422)
        self.admin_post(reference, "pickup/confirm", {**base, "claimant_type": "authorized"}, expect=422)
        claimed = self.admin_post(reference, "pickup/confirm", {**base, "claimant_type": "authorized", "authorization_details": "Authorization letter + ID", "verification_code": code.lower()})
        self.assertEqual(claimed["status"], "Order Claimed")
        self.assertEqual(claimed["fulfillment"]["pickup_confirmation"]["claimant_name"], "Ana Reyes")
        self.admin_post(reference, "pickup/confirm", base, expect=409)
        self.assertEqual(self.admin_post(reference, "status", {"status": "Completed"})["status"], "Completed")
        self.assertIn("order_claimed", self.kinds(reference))

    def test_unpaid_orders_are_never_released(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.to_packaging(reference)
        waiting = self.admin_post(reference, "packaging", {"completed_quantity": 2})
        self.assertEqual(waiting["status"], "Awaiting Full Payment")
        self.assertIn("balance_due", self.kinds(reference))
        self.assertEqual(waiting["payment"]["next_payment"]["stage"], "balance")
        self.admin_post(reference, "pickup/confirm", {"claimant_name": "X", "claimant_type": "customer", "verification_code": reference, "acknowledged": True}, expect=409)
        self.admin_post(reference, "pickup/schedule", {"pickup_date": "2026-12-01", "pickup_time": "1–5 PM"})
        released = self.pay_balance(reference)
        self.assertEqual(released["status"], "Ready for Pickup")
        self.assertEqual(released["fulfillment"]["pickup_schedule"]["date"], "2026-12-01")

    def test_legacy_released_orders_can_still_be_completed(self) -> None:
        legacy = workflow.ProductionAndFulfillmentTests.legacy_order(self, "Ready for Pickup", 300)
        self.admin_post(legacy, "payments", {"amount": 300, "method": "Cash", "received": True})
        self.assertIn("Completed", self.admin_view(legacy)["allowed_statuses"])
        self.assertEqual(self.admin_post(legacy, "status", {"status": "Completed"})["status"], "Completed")


# ====================================================================== delivery


class DeliveryTests(OperationsTestCase):
    def ready_for_dispatch(self) -> str:
        reference = self.confirmed_order(1000, fulfillment=workflow.DELIVERY)["reference"]
        self.pay_balance(reference)
        self.to_packaging(reference)
        self.assertEqual(self.admin_post(reference, "packaging", {"completed_quantity": 2})["status"], "Ready for Dispatch")
        return reference

    def test_dispatch_failure_reschedule_and_delivery_keep_every_attempt(self) -> None:
        reference = self.ready_for_dispatch()
        self.admin_post(reference, "dispatch", {}, expect=422)
        self.admin_post(reference, "dispatch", {"tracking_number": "12"}, expect=422)
        first = tracking()
        out = self.admin_post(reference, "dispatch", {"tracking_number": first, "service": "Express", "shipment_date": "2026-11-21"})
        self.assertEqual((out["status"], out["fulfillment"]["tracking_number"], out["fulfillment"]["courier"]), ("Out for Delivery", first, "J&T Express"))
        self.assertIn("out_for_delivery", self.kinds(reference))
        # Another order cannot reuse the tracking number.
        other = self.ready_for_dispatch()
        self.admin_post(other, "dispatch", {"tracking_number": first}, expect=409)

        self.admin_post(reference, "delivery/failed", {"reason": "teleported"}, expect=422)
        self.admin_post(reference, "delivery/failed", {"reason": "other"}, expect=422)
        failed = self.admin_post(reference, "delivery/failed", {"reason": "recipient_unavailable", "remarks": "Nobody home"})
        self.assertEqual(failed["status"], "Delivery Failed")
        self.assertEqual(failed["payment"]["balance"], 0.0)  # never refunded or cancelled automatically
        self.assertIn("delivery_failed", self.kinds(reference))
        self.admin_post(reference, "delivery/reschedule", {}, expect=422)
        self.assertEqual(self.admin_post(reference, "delivery/reschedule", {"scheduled_date": "2026-11-25"})["status"], "Ready for Dispatch")
        self.admin_post(reference, "dispatch", {"delivery_personnel": "Shop rider Ben"})
        self.admin_post(reference, "delivery/delivered", {"received_by": "Ana"}, expect=422)  # proof of delivery required
        delivered = self.admin_post(reference, "delivery/delivered", {"received_by": "Ana", "proof": proof("pod.png")})
        attempts = delivered["fulfillment"]["attempts"]
        self.assertEqual([item["status"] for item in attempts], ["failed", "delivered"])
        self.assertEqual(attempts[0]["failure_label"], "Recipient unavailable")
        self.admin_post(reference, "delivery/delivered", {"proof": proof()}, expect=409)
        history = self.admin.get("/api/admin/fulfillment", params={"tab": "history", "search": reference}).json()
        self.assertGreaterEqual(history["total"], 2)
        self.assertEqual(self.admin_post(reference, "status", {"status": "Completed"})["status"], "Completed")

    def test_customers_report_delivery_issues_and_see_tracking(self) -> None:
        reference = self.ready_for_dispatch()
        self.customer_post(reference, "delivery-issue", {"type": "damaged", "details": "x"}, expect=409)
        number = tracking()
        self.admin_post(reference, "dispatch", {"tracking_number": number})
        portal = self.shopper.get(f"/api/account/orders/{reference}").json()
        self.assertEqual(portal["fulfillment"]["tracking_number"], number)
        self.assertTrue(portal["fulfillment"]["delivery_disclaimer"])
        self.assertTrue(portal["customer_actions"]["report_delivery_issue"])
        self.customer_post(reference, "delivery-issue", {"type": "unknown", "details": "Late"}, expect=422)
        reported = self.customer_post(reference, "delivery-issue", {"type": "late", "details": "Not here yet"})
        issue = reported["fulfillment"]["issues"][0]
        self.customer_post(reference, "delivery-issue", {"type": "late", "details": "Again"}, expect=409)
        issues = self.admin.get("/api/admin/fulfillment", params={"tab": "issues", "search": reference}).json()
        self.assertEqual(issues["total"], 1)
        resolved = self.admin_post(reference, f"issues/{issue['id']}/resolve", {"resolution": "Courier confirmed arrival tomorrow"})
        self.assertEqual(resolved["fulfillment"]["issues"][0]["status"], "resolved")

    def test_fulfillment_and_production_lists(self) -> None:
        reference = self.ready_for_dispatch()
        overview = self.admin.get("/api/admin/fulfillment").json()
        self.assertGreaterEqual(overview["summary"]["counts"]["Ready for Dispatch"], 1)
        delivery = self.admin.get("/api/admin/fulfillment", params={"tab": "delivery", "search": reference}).json()
        self.assertEqual(delivery["items"][0]["reference"], reference)
        self.assertEqual(self.admin.get("/api/admin/fulfillment", params={"tab": "nope"}).status_code, 422)
        production = self.admin.get("/api/admin/production", params={"tab": "packaging"}).json()
        self.assertIn("counts", production)
        self.assertIn(self.shopper.get("/api/admin/fulfillment").status_code, {401, 403})


# ====================================================================== cancellation


class CancellationTests(OperationsTestCase):
    def request(self, reference: str, expect: int = 200, **body) -> dict:
        payload = {"reason": "change_of_mind", "acknowledged": True, **RECIPIENT, **body}
        return self.customer_post(reference, "cancellation-request", payload, expect)

    def test_unpaid_orders_cancel_directly(self) -> None:
        order = self.place()
        self.request(order["reference"], expect=409)
        self.assertEqual(self.customer_post(order["reference"], "cancel")["status"], "Cancelled")

    def test_paid_cancellation_is_reviewed_and_holds_the_order(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.request(reference, expect=422, acknowledged=False)
        self.request(reference, expect=422, recipient_name="")
        requested = self.request(reference, remarks="Event moved")
        self.assertEqual(requested["status"], "Confirmed")
        self.assertFalse(requested["customer_actions"]["request_cancellation"])
        self.request(reference, expect=409)
        # Production, drafts, and payments are on hold while the request is reviewed.
        self.admin_post(reference, "status", {"status": "Draft Preparation"}, expect=409)
        self.admin_post(reference, "draft", {"file": {"name": "d.png", "dataUrl": PNG}}, expect=409)
        self.submit_payment(reference, expect=409)
        # Another customer cannot see or cancel this order.
        stranger, _ = self.new_customer()
        stranger.post(f"/api/orders/{reference}/cancellation-request", json={"reason": "other"})
        self.assertEqual(stranger.post(f"/api/orders/{reference}/cancellation-request", json={"reason": "other"}).status_code, 404)

        listed = self.admin.get("/api/admin/cancellations", params={"search": reference}).json()
        request_id = listed["items"][0]["id"]
        self.assertEqual(listed["items"][0]["policy"]["stage"], "Paid, draft not started")
        self.admin_post(reference, f"cancellations/{request_id}/approve", {"deductions": [{"label": "Design work", "amount": 100}]}, expect=422)
        self.admin_post(reference, f"cancellations/{request_id}/approve", {"refund_amount": 600}, expect=422)
        approved = self.admin_post(reference, f"cancellations/{request_id}/approve", {
            "deductions": [{"label": "Artwork setup", "amount": 100}], "deduction_justification": "Initial layout prepared",
        })
        self.assertEqual(approved["status"], "Cancelled")
        refund = approved["refunds"][0]
        self.assertEqual((refund["status"], refund["amount"], refund["reason"]), ("approved", 400.0, "cancellation"))
        self.assertEqual(approved["payments"][0]["amount"], 500.0)  # payment history is never rewritten
        self.admin_post(reference, f"cancellations/{request_id}/reject", {"remarks": "late"}, expect=409)
        self.assertIn("cancellation_approved", self.kinds(reference))

    def test_rejected_cancellation_lets_the_order_continue(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        request_id = self.request(reference)["cancellation_requests"][0]["id"]
        self.admin_post(reference, f"cancellations/{request_id}/reject", {}, expect=422)
        rejected = self.admin_post(reference, f"cancellations/{request_id}/reject", {"remarks": "Printing already scheduled"})
        self.assertEqual((rejected["status"], rejected["cancellation_requests"][0]["status"]), ("Confirmed", "rejected"))
        self.assertEqual(self.admin_post(reference, "status", {"status": "Draft Preparation"})["status"], "Draft Preparation")

    def test_pending_payment_verification_is_resolved_first(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.submit_payment(reference)
        request_id = self.request(reference)["cancellation_requests"][0]["id"]
        self.admin_post(reference, f"cancellations/{request_id}/approve", {}, expect=409)
        self.verify(reference)
        approved = self.admin_post(reference, f"cancellations/{request_id}/approve", {})
        self.assertEqual(approved["refunds"][0]["amount"], 1000.0)


# ====================================================================== refunds


class RefundTests(OperationsTestCase):
    def cancelled_with_refund(self, amount: float = 1000) -> tuple[str, str]:
        reference = self.confirmed_order(amount)["reference"]
        request_id = self.customer_post(reference, "cancellation-request", {"reason": "financial", "acknowledged": True, **RECIPIENT})["cancellation_requests"][0]["id"]
        approved = self.admin_post(reference, f"cancellations/{request_id}/approve", {})
        return reference, approved["refunds"][0]["id"]

    def complete_body(self, **extra) -> dict:
        return {"confirmed": True, "recipient_verified": True, "method": "GCash", "transaction_reference": workflow.unique_reference(), "proof": proof("refund.png"), **extra}

    def test_full_refund_lifecycle_requires_a_confirmed_transfer(self) -> None:
        reference, refund_id = self.cancelled_with_refund()
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(), expect=409)  # not processing yet
        self.admin_post(reference, f"refunds/{refund_id}/processing")
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(confirmed=False), expect=422)
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(recipient_verified=False), expect=422)
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(proof=[]), expect=422)
        transaction = workflow.unique_reference()
        done = self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(transaction_reference=transaction))
        refund = done["refunds"][0]
        self.assertEqual((refund["status"], refund["transaction_reference"], done["payment"]["refunded"]), ("refunded", transaction, 500.0))
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body(), expect=409)
        self.assertIn("refund_completed", self.kinds(reference))
        # Another refund cannot reuse the transfer reference.
        other, other_id = self.cancelled_with_refund()
        self.admin_post(other, f"refunds/{other_id}/processing")
        self.admin_post(other, f"refunds/{other_id}/complete", self.complete_body(transaction_reference=transaction), expect=409)
        # Customers cannot process refunds.
        self.assertIn(self.shopper.post(f"/api/admin/orders/{other}/refunds/{other_id}/fail", json={"reason": "x"}).status_code, {401, 403})

    def test_failed_refunds_are_retried_safely(self) -> None:
        reference, refund_id = self.cancelled_with_refund()
        self.admin_post(reference, f"refunds/{refund_id}/processing")
        failed = self.admin_post(reference, f"refunds/{refund_id}/fail", {"reason": "Recipient number inactive"})
        self.assertEqual((failed["refunds"][0]["status"], failed["payment"]["refunded"]), ("failed", 0.0))
        self.assertIn("refund_failed", self.kinds(reference))
        self.admin_post(reference, f"refunds/{refund_id}/processing", {}, expect=422)
        self.admin_post(reference, f"refunds/{refund_id}/processing", {"confirmed_not_sent": True})
        self.assertEqual(self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body())["refunds"][0]["status"], "refunded")

    def test_duplicate_payment_refund_does_not_cancel_and_cannot_exceed_verified_payments(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        requested = self.customer_post(reference, "refund-request", {"reason": "duplicate_payment", "details": "I sent the down payment twice", "amount": 500, **RECIPIENT})
        self.assertEqual(requested["status"], "Confirmed")
        refund_id = requested["refunds"][0]["id"]
        self.customer_post(reference, "refund-request", {"reason": "excess_payment", "details": "x", **RECIPIENT}, expect=409)
        self.admin_post(reference, f"refunds/{refund_id}/approve", {"amount": 600}, expect=422)
        approved = self.admin_post(reference, f"refunds/{refund_id}/approve", {"amount": 200})
        self.assertEqual((approved["status"], approved["refunds"][0]["amount"]), ("Confirmed", 200.0))
        # 500 verified − 200 approved = 300 left; a shop-opened refund cannot exceed it.
        opened = self.admin_post(reference, "refunds", {"reason": "excess_payment", "details": "Overpaid via bank"})
        second = opened["refunds"][1]["id"]
        self.admin_post(reference, f"refunds/{second}/approve", {"amount": 301}, expect=422)
        self.admin_post(reference, f"refunds/{second}/not-eligible", {"remarks": "Not an excess payment"})
        self.assertEqual(self.admin_view(reference)["max_refundable"], 300.0)

    def test_damaged_products_can_be_reprinted_only_after_receipt(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.customer_post(reference, "refund-request", {"reason": "damaged_product", "details": "Torn", **RECIPIENT}, expect=409)
        self.pay_balance(reference)
        self.to_packaging(reference)
        code_view = self.admin_post(reference, "packaging", {"completed_quantity": 2})
        self.assertEqual(code_view["status"], "Ready for Pickup")
        code = self.shopper.get(f"/api/account/orders/{reference}").json()["fulfillment"]["pickup_code"]
        self.admin_post(reference, "pickup/confirm", {"claimant_name": "Me", "claimant_type": "customer", "verification_code": code, "acknowledged": True})
        requested = self.customer_post(reference, "refund-request", {
            "reason": "damaged_product", "details": "Two pieces torn", "preferred_resolution": "reprint", "evidence": proof("torn.png"),
        })
        refund = requested["refunds"][0]
        self.assertEqual(len(refund["evidence"]), 1)
        self.admin_post(reference, f"refunds/{refund['id']}/approve", {"resolution": "reprint"}, expect=422)
        # The administrator must authorize the reprint.
        self.admin_post(reference, f"refunds/{refund['id']}/approve", {"resolution": "reprint", "remarks": "Reprinting 2 pcs"}, expect=422)
        resolved = self.admin_post(reference, f"refunds/{refund['id']}/approve", {"resolution": "reprint", "remarks": "Reprinting 2 pcs", "authorized": True})
        self.assertEqual(resolved["refunds"][0]["status"], "reprint")
        # The reprint restarts production as a linked cycle; the refund points at it.
        self.assertEqual(resolved["status"], "In Production")
        self.assertEqual(resolved["refunds"][0]["reprint_id"], resolved["production"]["reprint"]["id"])
        self.assertEqual(resolved["production"]["reprint"]["refund_id"], refund["id"])
        self.assertEqual(resolved["payment"]["refunded"], 0.0)

    def test_reports_deduct_only_completed_refunds(self) -> None:
        reference, refund_id = self.cancelled_with_refund(800)
        pending = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((pending["verified_collections"], pending["refunds_completed"], pending["net_collections"]), (400.0, 0.0, 400.0))
        self.assertEqual(pending["refunds_in_progress"], 400.0)
        self.admin_post(reference, f"refunds/{refund_id}/processing")
        self.admin_post(reference, f"refunds/{refund_id}/complete", self.complete_body())
        done = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((done["verified_collections"], done["refunds_completed"], done["net_collections"]), (400.0, 400.0, 0.0))
        refunds = self.admin.get("/api/admin/refunds", params={"view": "history", "search": reference}).json()
        self.assertEqual(refunds["items"][0]["status"], "refunded")
        dashboard = self.admin.get("/api/admin/dashboard").json()
        for key in ("status_counts", "pending_cancellations", "pending_refunds", "collections"):
            self.assertIn(key, dashboard)
        counts = self.admin.get("/api/admin/counts").json()
        for key in ("pending_refunds", "pending_cancellations", "ready_for_dispatch", "delivery_failed", "refunds"):
            self.assertIn(key, counts)


if __name__ == "__main__":
    unittest.main()
