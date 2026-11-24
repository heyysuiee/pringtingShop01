"""Additive down-payment → finished-work approval → remaining-balance workflow.

The older draft-first flow stays untouched: an order only waits for work approval once the shop has actually
sent a finished-work preview. These tests exercise that new path through the HTTP API.
"""
from __future__ import annotations

import unittest

from backend.tests import test_order_workflow as workflow
from backend.tests.support import stored_order


PNG = workflow.PNG


class DownPaymentWorkApprovalTests(workflow.WorkflowTestCase):
    def preview(self, reference: str, remarks: str = "Finished work ready") -> dict:
        return self.admin_post(reference, "work-preview", {
            "file": {"name": "finished.png", "dataUrl": PNG},
            "remarks": remarks,
        })

    def test_admin_set_down_payment_is_enforced_and_cannot_exceed_total(self) -> None:
        quoted = self.customized_order(1000)
        reference = quoted["reference"]
        self.assertEqual(quoted["payment"]["down_payment_required"], 500.0)

        invalid = self.admin_post(
            reference, "quotation",
            {"total_amount": 1000, "down_payment_amount": 1001, "expected_version": quoted["version"]},
            expect=422,
        )
        self.assertIn("cannot exceed", invalid["detail"])

        revised = self.admin_post(reference, "quotation", {"total_amount": 1000, "down_payment_amount": 300})
        accepted = self.accept(revised)
        self.assertEqual(accepted["payment"]["down_payment_required"], 300.0)
        self.assertEqual(accepted["payment"]["next_payment"]["amount"], 300.0)
        self.assertTrue(accepted["payment"]["admin_set_down_payment"])

        self.submit_payment(reference)
        confirmed = self.verify(reference)
        self.assertEqual(confirmed["payment"]["down_payment_status"], "Paid")
        self.assertEqual(confirmed["payment"]["remaining_after_down_payment"], 700.0)
        self.assertEqual(confirmed["payment"]["balance"], 700.0)

    def test_package_payment_terms_can_be_set_before_the_down_payment(self) -> None:
        package = self.place_package()
        reference = package["reference"]
        approved = self.admin_post(reference, "confirm", {"payment_instructions": "GCash"})
        self.assertEqual(approved["payment"]["down_payment_required"], round(approved["total_amount"] * 0.5, 2))

        revised = self.admin_post(reference, "payment-terms", {
            "total_amount": 1200, "down_payment_amount": 200, "note": "Corrected quotation",
        })
        self.assertEqual(revised["total_amount"], 1200.0)
        self.assertEqual(revised["payment"]["down_payment_required"], 200.0)
        self.assertEqual(revised["payment"]["next_payment"]["amount"], 200.0)
        self.assertEqual(revised["payment"]["balance"], 1200.0)

    def test_finished_work_revision_and_approval_control_final_payment(self) -> None:
        reference = self.confirmed_order(1000)["reference"]

        previewed = self.preview(reference)
        self.assertEqual(previewed["status"], "Ready for Customer Review")
        self.assertEqual(previewed["work_approval"]["status"], "pending")
        self.assertEqual(len(previewed["work_approval"]["preview_files"]), 1)
        self.assertTrue(previewed["customer_actions"]["approve_work"])
        self.assertTrue(previewed["payment"]["final_payment_locked"])
        self.assertTrue(previewed["payment"]["next_payment"]["locked"])
        self.assertIn("Approve the finished work", previewed["payment"]["final_payment_lock"])

        # Neither the customer nor the administrator can move the balance before approval.
        blocked = self.submit_payment(reference, expect=409)
        self.assertIn("Approve the finished work", blocked["detail"])
        self.assertEqual(self.admin_view(reference)["actions"]["record_payment"], False)

        revised = self.customer_post(reference, "work/revision", {"note": "Change the background colour"})
        self.assertEqual(revised["status"], "Revision Requested")
        self.assertEqual(revised["work_approval"]["status"], "revision_requested")
        self.assertEqual(revised["work_approval"]["revision_note"], "Change the background colour")
        self.assertTrue(revised["payment"]["final_payment_locked"])

        resent = self.preview(reference, "Updated preview")
        self.assertEqual(resent["status"], "Ready for Customer Review")
        self.assertEqual(resent["work_approval"]["revision_count"], 1)

        approved = self.customer_post(reference, "work/approve", {"note": "Looks good"})
        self.assertEqual(approved["status"], "Awaiting Final Payment")
        self.assertEqual(approved["work_approval"]["status"], "approved")
        self.assertTrue(approved["payment"]["work_approved"])
        self.assertFalse(approved["payment"]["final_payment_locked"])
        self.assertEqual(approved["payment"]["next_payment"], {
            "stage": "balance", "label": "Remaining balance", "amount": 500.0, "locked": False, "reason": "",
        })

        kinds = set(self.kinds(reference))
        self.assertTrue({"work_ready_for_review", "work_revision_requested", "work_approved"} <= kinds)

        # A customer may report any positive amount, but the declared value is only a pending claim: it
        # never credits money or changes the server-calculated balance. Reject it so the real proof can be
        # submitted next.
        wrong = self.submit_payment(reference, amount=1)
        wrong_submission = next(
            item for item in wrong["payment_submissions"]
            if item["amount"] == 1 and item["status"] == "pending"
        )
        self.assertEqual(wrong["payment"]["amount_paid"], 500.0)
        self.assertEqual(wrong["payment"]["payment_status"], "Down Payment Paid")
        self.admin_post(reference, f"payment-submissions/{wrong_submission['id']}/reject", {
            "note": "Wrong amount",
        })
        reference_number = workflow.unique_reference()
        first = self.submit_payment(reference, payment_reference=reference_number)
        submission_id = next(item for item in first["payment_submissions"] if item["status"] == "pending")["id"]
        self.assertEqual(self.submit_payment(reference, payment_reference=reference_number, expect=409)["detail"],
                         "A payment is already waiting for the shop to verify it.")
        rejected = self.admin_post(reference, f"payment-submissions/{submission_id}/reject", {
            "note": "Receipt was unreadable",
        })
        self.assertEqual(rejected["payment"]["payment_status"], "Down Payment Paid")
        self.assertEqual(rejected["payment"]["balance"], 500.0)

        resubmitted = self.submit_payment(reference, payment_reference=reference_number)
        pending = next(item for item in resubmitted["payment_submissions"] if item["status"] == "pending")
        self.assertEqual(pending["reference"], reference_number)
        paid = self.verify(reference)
        self.assertEqual(paid["payment"]["payment_status"], "Fully Paid")
        self.assertEqual(paid["payment"]["amount_paid"], 1000.0)
        self.assertEqual(len(paid["payments"]), 2)
        self.assertEqual(paid["status"], "Ready for Pickup")

    def test_review_status_without_a_preview_cannot_unlock_payment(self) -> None:
        reference = workflow.ProductionAndFulfillmentTests.legacy_order(self, "Ready for Customer Review", 1000)
        self.admin_post(reference, "payments", {"amount": 500, "method": "Cash", "received": True})
        view = self.admin_view(reference)
        self.assertTrue(view["payment"]["final_payment_locked"])
        self.assertFalse(view["actions"]["record_payment"])
        blocked = self.submit_payment(reference, expect=409)
        self.assertIn("Approve the finished work", blocked["detail"])

    def test_legacy_cod_orders_can_still_be_paid_in_instalments(self) -> None:
        reference = workflow.ProductionAndFulfillmentTests.legacy_order(self, "Ready for Pickup", 600, "cod")
        first = self.admin_post(reference, "payments", {"amount": 300, "method": "Cash", "received": True})
        self.assertEqual(first["payment"]["balance"], 300.0)
        paid = self.admin_post(reference, "payments", {"amount": 300, "method": "Cash", "received": True})
        self.assertEqual(paid["payment"]["payment_status"], "Fully Paid")
        self.assertEqual(len(paid["payments"]), 2)

    def test_legacy_orders_without_a_preview_keep_their_existing_payment_flow(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        view = self.admin_view(reference)
        self.assertEqual(view["work_approval"]["status"], "")
        self.assertFalse(view["payment"]["final_payment_locked"])
        self.assertTrue(view["actions"]["record_payment"])
        self.submit_payment(reference)
        paid = self.verify(reference)
        self.assertEqual(paid["payment"]["payment_status"], "Fully Paid")
        self.assertEqual(len(paid["payments"]), 2)


    def test_payment_terms_default_down_payment_notifies_and_audits(self) -> None:
        package = self.place_package()
        reference = package["reference"]
        approved = self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000"})
        before = self.admin_view(reference)
        new_total = round(float(approved["total_amount"]) + 137.5, 2)
        expected_down = round(new_total * 0.5, 2)

        revised = self.admin_post(reference, "payment-terms", {
            "total_amount": new_total,
            "down_payment_amount": None,
            "payment_instructions": "GCash 0917 000 0000",
            "note": "Corrected total",
        })

        self.assertEqual(revised["total_amount"], new_total)
        self.assertEqual(revised["payment"]["down_payment_required"], expected_down)
        self.assertEqual(revised["payment"]["remaining_after_down_payment"], expected_down)
        self.assertEqual(revised["payment"]["next_payment"]["amount"], expected_down)
        self.assertFalse(revised["payment"]["admin_set_down_payment"])
        self.assertTrue(any("50% down payment" in item["text"] and "Reason: Corrected total" in item["text"] for item in revised["history"]))

        notice = next(item for item in self.notifications(reference=reference) if item["kind"] == "payment_terms_updated")
        self.assertEqual(notice["title"], "Payment terms updated")
        self.assertIn(f"PHP {new_total:,.2f}", notice["message"])
        self.assertIn(f"PHP {expected_down:,.2f}", notice["message"])
        self.assertIn("GCash 0917 000 0000", notice["message"])

        entry = next(item for item in self.audit(reference) if item["action"] == "payment.terms_set")
        self.assertEqual(entry["actor_type"], "admin")
        self.assertTrue(entry["at"])
        self.assertEqual(entry["details"]["note"], "Corrected total")
        self.assertEqual(entry["details"]["down_payment"], expected_down)
        self.assertEqual(entry["details"]["down_payment_source"], "default_50_percent")
        self.assertEqual(entry["details"]["remaining_after_down_payment"], expected_down)
        self.assertEqual(entry["details"]["total_from"], before["total_amount"])
        self.assertEqual(entry["details"]["total_to"], new_total)

        after = self.admin_view(reference)
        self.assertEqual(after["pricing"], before["pricing"])
        self.assertEqual(after["quotation_history"], before["quotation_history"])
        self.assertEqual(stored_order(reference)["pricing"], before["pricing"])

    def test_payment_terms_custom_down_payment_records_full_breakdown(self) -> None:
        package = self.place_package()
        reference = package["reference"]
        approved = self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000"})
        new_total = round(float(approved["total_amount"]) + 200, 2)
        custom_down = 125.0

        revised = self.admin_post(reference, "payment-terms", {
            "total_amount": new_total,
            "down_payment_amount": custom_down,
            "payment_instructions": "GCash 0917 000 0000",
            "note": "Customer requested a smaller deposit",
        })

        self.assertEqual(revised["payment"]["down_payment_required"], custom_down)
        self.assertEqual(revised["payment"]["remaining_after_down_payment"], round(new_total - custom_down, 2))
        self.assertEqual(revised["payment"]["next_payment"]["amount"], custom_down)
        self.assertTrue(revised["payment"]["admin_set_down_payment"])

        entry = next(item for item in self.audit(reference) if item["action"] == "payment.terms_set")
        self.assertEqual(entry["details"]["down_payment"], custom_down)
        self.assertEqual(entry["details"]["down_payment_source"], "custom")
        self.assertEqual(entry["details"]["remaining_after_down_payment"], round(new_total - custom_down, 2))
        self.assertEqual(entry["details"]["note"], "Customer requested a smaller deposit")

        notice = next(item for item in self.notifications(reference=reference) if item["kind"] == "payment_terms_updated")
        self.assertIn(f"custom down payment PHP {custom_down:,.2f}", notice["message"])

    def test_payment_terms_validation_failure_leaves_no_trace(self) -> None:
        package = self.place_package()
        reference = package["reference"]
        approved = self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000"})
        before = self.admin_view(reference)
        audit_count = len(self.audit(reference))
        notice_count = len(self.notifications(reference=reference))
        new_total = round(float(approved["total_amount"]) + 137.5, 2)
        base = {"payment_instructions": "GCash 0917 000 0000", "note": "Bad attempt"}
        cases = [
            {"total_amount": new_total, "down_payment_amount": 0},
            {"total_amount": new_total, "down_payment_amount": -5},
            {"total_amount": new_total, "down_payment_amount": new_total + 1},
            {"total_amount": new_total, "down_payment_amount": "not-a-number"},
            {"total_amount": "", "down_payment_amount": None},
            {"total_amount": new_total, "down_payment_amount": None, "note": ""},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.admin_post(reference, "payment-terms", {**base, **case}, expect=422)

        after = self.admin_view(reference)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["total_amount"], before["total_amount"])
        self.assertEqual(after["required_down_payment"], before["required_down_payment"])
        self.assertEqual(after["pricing"], before["pricing"])
        self.assertEqual(len(self.audit(reference)), audit_count)
        self.assertEqual(len(self.notifications(reference=reference)), notice_count)
        self.assertFalse(any(item["kind"] == "payment_terms_updated" for item in self.notifications(reference=reference)))

    def test_payment_terms_locked_after_payment_is_submitted_or_recorded(self) -> None:
        package = self.place_package()
        reference = package["reference"]
        self.admin_post(reference, "confirm", {"payment_instructions": "GCash 0917 000 0000"})
        self.submit_payment(reference)
        attempt = {"total_amount": 9999, "down_payment_amount": 100, "note": "After payment"}

        blocked = self.admin_post(reference, "payment-terms", attempt, expect=409)
        self.assertIn("payment", blocked["detail"].lower())

        pending = self.pending_submission(reference)
        self.admin_post(reference, f"payment-submissions/{pending['id']}/reject", {"note": "Unreadable receipt"})
        self.admin_post(reference, "payments", {"amount": 100, "method": "Cash", "received": True})
        self.admin_post(reference, "payment-terms", attempt, expect=409)

        self.assertFalse(any(item["action"] == "payment.terms_set" for item in self.audit(reference)))
        self.assertFalse(any(item["kind"] == "payment_terms_updated" for item in self.notifications(reference=reference)))


if __name__ == "__main__":
    unittest.main()
