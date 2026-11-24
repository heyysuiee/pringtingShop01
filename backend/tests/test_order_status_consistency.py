"""Focused regression tests for separate, synchronized order status dimensions.

Current fulfillment progress (``fulfillment_status``) must never show a release destination
(``next_release_status``) as if it had already happened, and packaging or payment verification
must never put an order into a Ready status while money still needs review.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-status-consistency-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402

from backend.app.services import order_services  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def _order(**overrides) -> dict:
    order = {
        "reference": "TEST-STATUS-1",
        "status": "Awaiting Full Payment",
        "created_at": "2026-11-24T00:00:00+00:00",
        "updated_at": "2026-11-24T00:00:00+00:00",
        "total_amount": 1000.0,
        "payment_option": "down_payment",
        "required_down_payment": 500.0,
        "payments": [{"amount": 1000.0, "applied_amount": 1000.0, "excess_amount": 0.0}],
        "payment_submissions": [],
        "cancellation_requests": [],
        "fulfillment": {"method": "pickup"},
        "work_approval": {},
        "drafts": [],
    }
    order.update(overrides)
    return order


class ReleaseReadinessTests(unittest.TestCase):
    def test_release_requires_full_payment(self) -> None:
        partial = _order(payments=[{"amount": 500.0, "applied_amount": 500.0, "excess_amount": 0.0}])
        self.assertFalse(order_services.release_ready(partial))
        with self.assertRaises(HTTPException) as caught:
            order_services.ensure_release_ready(partial, "release this order")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("remaining balance", caught.exception.detail)

    def test_unreviewed_excess_is_never_counted_as_paid(self) -> None:
        excess = _order(payments=[{
            "amount": 1000.51,
            "applied_amount": 1000.0,
            "excess_amount": 0.51,
            "requires_admin_review": True,
            "excess_review_status": "pending",
        }])
        self.assertFalse(order_services.release_ready(excess))
        with self.assertRaises(HTTPException) as caught:
            order_services.ensure_release_ready(excess, "release this order")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("flagged excess", caught.exception.detail)

    def test_pending_cancellation_blocks_release(self) -> None:
        cancelling = _order(cancellation_requests=[{"status": "pending_review"}])
        self.assertFalse(order_services.release_ready(cancelling))
        with self.assertRaises(HTTPException) as caught:
            order_services.ensure_release_ready(cancelling, "release this order")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("cancellation request", caught.exception.detail)

    def test_release_destination_is_not_current_progress(self) -> None:
        cases = {
            "Awaiting Down Payment": "Not started",
            "Confirmed": "Not started",
            "Draft Preparation": "Not started",
            "Approved for Printing": "In production",
            "In Production": "In production",
            "Quality Checking": "Quality checking",
            "Packaging": "Packaging",
            "Awaiting Full Payment": "Awaiting full payment",
            "Ready for Pickup": "Ready for pickup",
            "Ready for Dispatch": "Ready for dispatch",
            "Out for Delivery": "Out for delivery",
            "Delivered": "Delivered",
            "Completed": "Completed",
            "Cancelled": "Not applicable",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                order = _order(status=status)
                self.assertEqual(order_services.fulfillment_status(order), expected)
                self.assertEqual(order_services.next_release_status(order), "Ready for Pickup")
                if status not in {"Ready for Pickup", "Ready for Dispatch"}:
                    self.assertNotIn("Ready for", order_services.fulfillment_status(order))


class StatusDisplayContractTests(unittest.TestCase):
    def test_admin_details_show_progress_and_a_separate_release_destination(self) -> None:
        source = _read("frontend/assets/admin/dashboard.js")
        self.assertIn('["Fulfillment status", badge(f.fulfillment_status || "Not started")]', source)
        self.assertIn('["Release destination", nextRelease]', source)
        self.assertNotIn('["Status", badge(f.release_status || "Ready for Pickup")]', source)

    def test_operations_packaging_uses_the_shared_release_gate(self) -> None:
        source = _read("frontend/assets/admin/operations-admin.js")
        self.assertIn("const releaseReady = f.release_ready === true;", source)
        self.assertIn("f.next_release_status || f.release_status", source)
        self.assertIn("Awaiting Full Payment", source)

    def test_completion_notice_uses_the_same_gate(self) -> None:
        backend = _read("backend/app/services/report_services.py")
        dashboard = _read("frontend/assets/admin/dashboard.js")
        self.assertIn('not order_services.release_ready(order)', backend)
        self.assertIn('Resolve any pending payment review or record the remaining balance', dashboard)

    def test_customer_portal_has_a_fulfillment_status_snapshot(self) -> None:
        html = _read("frontend/customer/customer-portal.html")
        script = _read("frontend/assets/portal.js")
        self.assertIn('id="snapshot-fulfillment-status"', html)
        self.assertIn('const fulfillmentStatus = order.fulfillment?.fulfillment_status || "Not started";', script)
        self.assertIn('setText("#snapshot-fulfillment-status", fulfillmentStatus)', script)
        self.assertIn('colourStatus("#snapshot-fulfillment-status", fulfillmentStatus)', script)


if __name__ == "__main__":
    unittest.main()
