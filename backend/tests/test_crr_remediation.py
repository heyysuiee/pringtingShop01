"""Regression tests for targeted cancellation/refund remediation (CRR-001/002/003)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-crr-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from backend.tests import test_order_workflow as workflow  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


class CRR001RecipientValidationTests(workflow.WorkflowTestCase):
    def test_pending_payment_cancellation_request_requires_recipient(self):
        ref = self.approved_package()["reference"]
        self.submit_payment(ref, payment_reference="TXN-CRR1-1")
        response = self.shopper.post(
            f"/api/orders/{ref}/cancellation-request",
            json={"email": self.email, "reason": "change_of_mind", "remarks": "x", "acknowledged": True},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("recipient", response.json()["detail"].lower())

    def test_verified_after_request_blocks_approval_without_recipient_and_allows_with_recipient(self):
        ref = self.approved_package()["reference"]
        self.submit_payment(ref, payment_reference="TXN-CRR1-2")
        # Simulate a legacy/premediation-gap request with missing recipient.
        order = stored_order(ref)
        requests = list(order.get("cancellation_requests") or [])
        requests.append({
            "id": "CR-LEGACY-1",
            "reason": "change_of_mind",
            "reason_label": "Changed my mind",
            "remarks": "x",
            "status": "pending_review",
            "requested_at": order["created_at"],
            "order_status": order["status"],
            "verified_paid": 0.0,
            "pending_verification": 187.5,
            "payment_methods": [],
            "refund_recipient": {},
            "acknowledged": True,
        })
        from backend.app.repositories import order_repository
        order_repository.update_order(ref, {"cancellation_requests": requests}, "Test inserted legacy cancellation request")
        sub_id = self.admin_view(ref)["payment_submissions"][0]["id"]
        self.admin_post(ref, f"payment-submissions/{sub_id}/verify", {"received": True})
        blocked = self.admin.post(
            f"/api/admin/orders/{ref}/cancellations/CR-LEGACY-1/approve",
            json={"expected_version": self.admin_view(ref)["version"]},
        )
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertIn("recipient", blocked.json()["detail"].lower())
        self.assertEqual(self.admin_view(ref)["status"], "Confirmed")
        self.assertEqual(self.admin_view(ref)["refunds"], [])

        approved = self.admin_post(ref, "cancellations/CR-LEGACY-1/approve", {
            "refund_provider": "GCash",
            "recipient_name": "Workflow Customer",
            "recipient_number": "09171234567",
        })
        self.assertEqual(approved["status"], "Cancelled")
        self.assertEqual(approved["refunds"][0]["status"], "approved")
        self.assertEqual(approved["refunds"][0]["recipient"]["name"], "Workflow Customer")

    def test_zero_refund_cancellation_still_supported(self):
        ref = self.confirmed_order()["reference"]  # 1000 total, 500 paid
        cr = self.customer_post(ref, "cancellation-request", {
            "reason": "change_of_mind", "remarks": "x", "acknowledged": True,
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        req_id = cr["cancellation_requests"][0]["id"]
        approved = self.admin_post(ref, f"cancellations/{req_id}/approve", {
            "deductions": [{"label": "Work done", "amount": 500}],
            "deduction_justification": "Documented work",
            "completed_work": "Draft completed",
            "refund_amount": 0,
        })
        self.assertEqual(approved["status"], "Cancelled")
        self.assertEqual(approved["refunds"], [])


class CRR002PendingCancellationConflictTests(workflow.WorkflowTestCase):
    def test_customer_cannot_request_refund_while_cancellation_pending(self):
        ref = self.confirmed_order()["reference"]
        self.customer_post(ref, "cancellation-request", {
            "reason": "change_of_mind", "remarks": "x", "acknowledged": True,
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        response = self.shopper.post(f"/api/orders/{ref}/refund-request", json={
            "email": self.email, "reason": "duplicate_payment", "details": "dup",
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("pending review", response.json()["detail"].lower())

    def test_rejected_cancellation_does_not_block_legitimate_refund(self):
        ref = self.confirmed_order()["reference"]
        cr = self.customer_post(ref, "cancellation-request", {
            "reason": "change_of_mind", "remarks": "x", "acknowledged": True,
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        req_id = cr["cancellation_requests"][0]["id"]
        self.admin_post(ref, f"cancellations/{req_id}/reject", {"remarks": "Continue order"})
        rr = self.customer_post(ref, "refund-request", {
            "reason": "duplicate_payment", "details": "I was charged twice",
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        self.assertEqual(rr["refunds"][-1]["status"], "pending_review")


class CRR003DuplicateAdminRefundTests(workflow.WorkflowTestCase):
    def test_admin_create_refund_without_pending_request(self):
        ref = self.confirmed_order()["reference"]
        created = self.admin_post(ref, "refunds", {"reason": "excess_payment", "details": "Overpaid"})
        self.assertEqual(created["refunds"][-1]["status"], "pending_review")

    def test_admin_cannot_create_second_pending_refund(self):
        ref = self.confirmed_order()["reference"]
        self.admin_post(ref, "refunds", {"reason": "excess_payment", "details": "Overpaid"})
        response = self.admin.post(f"/api/admin/orders/{ref}/refunds", json={
            "reason": "duplicate_payment", "details": "Again", "expected_version": self.admin_view(ref)["version"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("pending review", response.json()["detail"].lower())

    def test_customer_pending_refund_blocks_admin_creation(self):
        ref = self.confirmed_order()["reference"]
        self.customer_post(ref, "refund-request", {
            "reason": "duplicate_payment", "details": "dup",
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        response = self.admin.post(f"/api/admin/orders/{ref}/refunds", json={
            "reason": "excess_payment", "details": "admin", "expected_version": self.admin_view(ref)["version"],
        })
        self.assertEqual(response.status_code, 409, response.text)

    def test_rejected_previous_refund_allows_new_legitimate_refund(self):
        ref = self.confirmed_order()["reference"]
        rr = self.customer_post(ref, "refund-request", {
            "reason": "duplicate_payment", "details": "dup",
            "refund_provider": "GCash", "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        rid = rr["refunds"][-1]["id"]
        self.admin_post(ref, f"refunds/{rid}/not-eligible", {"remarks": "Not eligible"})
        created = self.admin_post(ref, "refunds", {"reason": "excess_payment", "details": "Legit"})
        self.assertEqual(created["refunds"][-1]["status"], "pending_review")

    def test_multiple_legitimate_partial_refunds_after_resolution(self):
        ref = self.confirmed_order(amount=1000)["reference"]  # paid 500
        first = self.admin_post(ref, "refunds", {"reason": "excess_payment", "details": "first"})
        rid1 = first["refunds"][-1]["id"]
        self.admin_post(ref, f"refunds/{rid1}/approve", {"amount": 200})
        self.admin_post(ref, f"refunds/{rid1}/processing", {})
        self.admin_post(ref, f"refunds/{rid1}/complete", {
            "confirmed": True, "recipient_verified": True, "method": "GCash",
            "transaction_reference": "TXN-CRR3-P1", "proof": [{"name": "r.png", "dataUrl": workflow.PNG}],
            "recipient_name": "Workflow Customer", "recipient_number": "09171234567",
        })
        second = self.admin_post(ref, "refunds", {"reason": "duplicate_payment", "details": "second"})
        rid2 = second["refunds"][-1]["id"]
        self.admin_post(ref, f"refunds/{rid2}/approve", {"amount": 300})
        view = self.admin_view(ref)
        self.assertEqual(view["payment"]["refunded"], 200.0)
        self.assertEqual(view["max_refundable"], 0.0)

    def test_concurrent_duplicate_admin_creation(self):
        # Optimistic versioning should prevent duplicate pending refund creation.
        ref = self.confirmed_order()["reference"]
        v = self.admin_view(ref)["version"]
        r1 = self.admin.post(f"/api/admin/orders/{ref}/refunds", json={"reason": "excess_payment", "details": "one", "expected_version": v})
        r2 = self.admin.post(f"/api/admin/orders/{ref}/refunds", json={"reason": "duplicate_payment", "details": "two", "expected_version": v})
        codes = sorted([r1.status_code, r2.status_code])
        self.assertEqual(codes, [200, 409], f"{r1.status_code} {r1.text[:200]} | {r2.status_code} {r2.text[:200]}")
        view = self.admin_view(ref)
        self.assertEqual(sum(1 for r in view.get("refunds", []) if r["status"] == "pending_review"), 1)


if __name__ == "__main__":
    unittest.main()