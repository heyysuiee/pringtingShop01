"""Second audit: reprint workflow, financial accuracy, database integrity, concurrency, and security.

Uses the HTTP API as the customer and the administrator, like a browser would.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import date
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-audit-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import report_services  # noqa: E402
from backend.tests import test_operations as operations  # noqa: E402
from backend.tests import test_order_workflow as workflow  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


PNG = workflow.PNG
PROJECT = Path(__file__).resolve().parents[2]


class AuditTestCase(operations.OperationsTestCase):
    def delivered_order(self, amount: float = 1000, fulfillment: dict | None = None) -> str:
        """A fully paid delivery order that was delivered (status Delivered)."""
        reference = self.confirmed_order(amount, fulfillment=fulfillment or workflow.DELIVERY)["reference"]
        self.pay_balance(reference)
        self.to_packaging(reference)
        self.admin_post(reference, "packaging", {"completed_quantity": 2})
        self.admin_post(reference, "dispatch", {"tracking_number": operations.tracking()})
        self.admin_post(reference, "delivery/delivered", {"proof": operations.proof("pod.png")})
        return reference

    def claimed_order(self, amount: float = 1000) -> str:
        reference = self.confirmed_order(amount)["reference"]
        self.pay_balance(reference)
        self.to_packaging(reference)
        self.admin_post(reference, "packaging", {"completed_quantity": 2})
        code = self.shopper.get(f"/api/account/orders/{reference}").json()["fulfillment"]["pickup_code"]
        self.admin_post(reference, "pickup/confirm", {"claimant_name": "Me", "claimant_type": "customer", "verification_code": code, "acknowledged": True})
        return reference

    def reprint_body(self, **extra) -> dict:
        return {"reason": "Colors faded on 2 mugs", "authorized": True, **extra}


# ====================================================================== reprints


class ReprintTests(AuditTestCase):
    def test_free_reprint_restarts_production_and_keeps_history(self) -> None:
        reference = self.delivered_order()
        expected_total = float(stored_order(reference)["total_amount"])
        completed = self.admin_post(reference, "status", {"status": "Completed"})
        first_completed_at = stored_order(reference)["completed_at"]
        original_payments = completed["payments"]
        first_tracking = completed["fulfillment"]["tracking_number"]
        revenue_before = report_services.confirmed_revenue(stored_order(reference))
        self.assertEqual(revenue_before, expected_total)

        self.admin_post(reference, "reprint", {"reason": "Faded", "authorized": False}, expect=422)
        self.admin_post(reference, "reprint", {"authorized": True}, expect=422)  # reason required
        self.admin_post(reference, "reprint", self.reprint_body(quantity=99), expect=422)
        self.assertIn(self.shopper.post(f"/api/admin/orders/{reference}/reprint", json=self.reprint_body()).status_code, {401, 403})
        started = self.admin_post(reference, "reprint", self.reprint_body(quantity=1))
        self.assertEqual(started["status"], "In Production")
        reprint = started["production"]["reprint"]
        self.assertEqual((reprint["status"], reprint["quantity"], reprint["chargeable"], reprint["original_status"]), ("in_progress", 1, False, "Completed"))
        self.assertEqual(started["production"]["cycle"], 2)
        self.assertEqual(len(started["production"]["previous_cycles"]), 1)
        self.assertTrue(started["production"]["previous_cycles"][0]["quality_passed"])
        # Original payments, total, and the first delivery are kept; no new payment is due for a free reprint.
        self.assertEqual(started["payments"], original_payments)
        self.assertEqual((started["total_amount"], started["payment"]["balance"], started["payment"]["next_payment"]), (expected_total, 0.0, None))
        previous = started["fulfillment"]["previous_cycles"][0]
        self.assertEqual(previous["attempts"][0]["tracking_number"], first_tracking)
        self.assertEqual(started["fulfillment"]["attempts"], [])
        self.assertIn("reprint_started", self.kinds(reference))
        # Duplicate reprints are refused while one is in progress.
        self.admin_post(reference, "reprint", self.reprint_body(), expect=409)
        # Reports keep the original completion and revenue while the reprint is in progress.
        self.assertEqual(report_services.confirmed_revenue(stored_order(reference)), expected_total)
        row = report_services.order_row(stored_order(reference), report_services.ServiceIndex())
        self.assertTrue(row["reported_completed"] and row["reprint_in_progress"])

        # Quality checking and packaging are required again; the old tracking number cannot be reused.
        self.admin_post(reference, "packaging", {"completed_quantity": 1}, expect=409)
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        self.admin_post(reference, "quality-check", {"checks": operations.CHECKS_PASSED})
        self.admin_post(reference, "packaging", {"completed_quantity": 2}, expect=422)  # more than the reprinted quantity
        released = self.admin_post(reference, "packaging", {"completed_quantity": 1})
        self.assertEqual(released["status"], "Ready for Dispatch")  # fully paid: no payment step for a free reprint
        self.admin_post(reference, "dispatch", {"tracking_number": first_tracking}, expect=409)
        self.admin_post(reference, "dispatch", {"tracking_number": operations.tracking()})
        delivered = self.admin_post(reference, "delivery/delivered", {"proof": operations.proof()})
        self.assertEqual(delivered["production"]["reprint"]["status"], "completed")
        done = self.admin_post(reference, "status", {"status": "Completed"})
        self.assertEqual(done["status"], "Completed")
        self.assertEqual(stored_order(reference)["completed_at"], first_completed_at)  # historical date preserved
        # The first cycle's proof of delivery is still served.
        old_proof = done["fulfillment"]["previous_cycles"][0]["attempts"][0]["proof"][0]["url"]
        self.assertEqual(self.admin.get(old_proof).status_code, 200)

    def test_chargeable_reprint_needs_agreement_and_payment_before_release(self) -> None:
        reference = self.claimed_order()
        self.admin_post(reference, "reprint", self.reprint_body(chargeable=True, charge_amount=300), expect=422)
        self.admin_post(reference, "reprint", self.reprint_body(chargeable=True, customer_agreed=True), expect=422)
        started = self.admin_post(reference, "reprint", self.reprint_body(chargeable=True, charge_amount=300, customer_agreed=True))
        self.assertEqual((started["total_amount"], started["payment"]["balance"]), (1300.0, 300.0))
        self.assertIsNone(started["fulfillment"].get("pickup_confirmation"))
        self.admin_post(reference, "status", {"status": "Quality Checking"})
        self.admin_post(reference, "quality-check", {"checks": operations.CHECKS_PASSED})
        waiting = self.admin_post(reference, "packaging", {"completed_quantity": 2})
        self.assertEqual(waiting["status"], "Awaiting Full Payment")
        self.assertEqual(waiting["payment"]["next_payment"]["amount"], 300.0)
        released = self.pay_balance(reference)
        self.assertEqual(released["status"], "Ready for Pickup")
        new_code = self.shopper.get(f"/api/account/orders/{reference}").json()["fulfillment"]["pickup_code"]
        claimed = self.admin_post(reference, "pickup/confirm", {"claimant_name": "Me", "claimant_type": "customer", "verification_code": new_code, "acknowledged": True})
        self.assertEqual(claimed["production"]["reprint"]["status"], "completed")
        self.assertEqual(len(claimed["fulfillment"]["previous_cycles"]), 1)

    def test_refund_reprint_can_restart_at_draft_preparation_and_blocks_duplicates(self) -> None:
        reference = self.claimed_order()
        refund_id = self.customer_post(reference, "refund-request", {"reason": "incorrect_product", "details": "Wrong name printed", "preferred_resolution": "reprint"})["refunds"][0]["id"]
        resolved = self.admin_post(reference, f"refunds/{refund_id}/approve", {
            "resolution": "reprint", "remarks": "Correct the name and reprint", "authorized": True, "restart_stage": "Draft Preparation",
        })
        self.assertEqual(resolved["status"], "Draft Preparation")
        self.assertEqual(resolved["payment"]["refunded"], 0.0)
        # The customer cannot ask for another reprint while one is in progress; a refund for other reasons is still possible.
        self.customer_post(reference, "refund-request", {"reason": "damaged_product", "details": "x", "preferred_resolution": "reprint"}, expect=409)
        self.admin_post(reference, "reprint", self.reprint_body(), expect=409)
        # The corrected artwork goes through the normal draft approval again.
        self.admin_post(reference, "status", {"status": "In Production"}, expect=409)
        self.admin_post(reference, "draft", {"file": {"name": "corrected.png", "dataUrl": PNG}})
        approved = self.customer_post(reference, "approve")
        self.assertEqual(approved["status"], "Approved for Printing")
        self.assertEqual(len(approved["drafts"]), 2)

    def test_cancellation_during_chargeable_reprint_closes_the_reprint(self) -> None:
        reference = self.delivered_order(1000)
        self.admin_post(reference, "reprint", self.reprint_body(chargeable=True, charge_amount=300, customer_agreed=True))
        request_id = self.customer_post(reference, "cancellation-request", {
            "reason": "change_of_mind", "acknowledged": True, **operations.RECIPIENT,
        })["cancellation_requests"][0]["id"]
        # The unpaid reprint charge is not refundable: only verified payments are.
        self.admin_post(reference, f"cancellations/{request_id}/approve", {"refund_amount": 1300}, expect=422)
        approved = self.admin_post(reference, f"cancellations/{request_id}/approve", {"refund_amount": 1000})
        self.assertEqual(approved["status"], "Cancelled")
        reprint = approved["production"]["reprint"]
        self.assertEqual((reprint["status"], reprint["cancellation_request_id"]), ("cancelled", request_id))
        self.assertTrue(reprint["cancelled_at"])
        self.assertEqual(approved["refunds"][0]["amount"], 1000.0)
        # A cancelled order must not stay "completed" in reports through a dangling in-progress reprint.
        stored = stored_order(reference)
        self.assertFalse(report_services.is_confirmed_revenue(stored))
        self.assertEqual(report_services.confirmed_revenue(stored), 0.0)
        self.assertIsNone(report_services.completion_date(stored))
        row = report_services.order_row(stored, report_services.ServiceIndex())
        self.assertFalse(row["reprint_in_progress"])
        self.assertFalse(row["reported_completed"])

    def test_customer_report_keeps_completion_during_reprint(self) -> None:
        reference = self.delivered_order(1000)
        self.admin_post(reference, "status", {"status": "Completed"})
        customer_id = stored_order(reference)["customer_id"]
        before = report_services.customer_detail(customer_id)
        self.admin_post(reference, "reprint", self.reprint_body(chargeable=True, charge_amount=300, customer_agreed=True))
        # The customer report uses the same completion rule as the order report and the revenue it shows.
        after = report_services.customer_detail(customer_id)
        completed = lambda detail: sum(item["completed"] for item in detail["service_history"])
        self.assertEqual(after["customer"]["completed_orders"] - before["customer"]["completed_orders"], 0)
        self.assertEqual(completed(after) - completed(before), 0)
        # Revenue is unchanged until the reprint is completed; the new charge is not revenue yet.
        self.assertEqual(round(after["customer"]["total_spent"] - before["customer"]["total_spent"], 2), 0.0)
        self.assertEqual(round(after["customer"]["total_paid"] - before["customer"]["total_paid"], 2), 0.0)


# ====================================================================== financial accuracy


class FinancialAccuracyTests(AuditTestCase):
    def test_multiple_payments_down_payment_and_balance(self) -> None:
        reference = self.accept(self.customized_order(1001))["reference"]
        first = self.admin_post(reference, "payments", {"amount": 200.25, "method": "Cash", "received": True})
        self.assertEqual((first["status"], first["payment"]["down_payment_remaining"], first["payment"]["balance"]), ("Awaiting Down Payment", 300.25, 800.75))
        second = self.admin_post(reference, "payments", {"amount": 300.25, "method": "GCash", "reference": workflow.unique_reference(), "received": True})
        self.assertEqual((second["status"], second["payment"]["balance"]), ("Confirmed", 500.5))
        # An excess is accepted only as received money flagged for review; it is covered in its own test
        # below so it cannot change the expected totals for this ordinary two-payment order.
        third = self.admin_post(reference, "payments", {"amount": 500.5, "method": "Cash", "received": True})
        self.assertEqual((third["payment"]["balance"], third["payment"]["next_payment"]), (0.0, None))
        self.submit_payment(reference, expect=409)  # nothing left to pay
        summary = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((summary["verified_collections"], summary["down_payments"], summary["balance_payments"]), (1001.0, 500.5, 500.5))

    def test_admin_overpayment_is_held_for_review_and_not_counted(self) -> None:
        reference = self.accept(self.customized_order(1000))["reference"]
        self.admin_post(reference, "payments", {"amount": 500, "method": "Cash", "received": True})

        over = self.admin_post(reference, "payments", {"amount": 500.51, "method": "Cash", "received": True})
        payment = over["payments"][-1]
        # The full receipt is kept for the audit trail, but only the remaining balance is credited.
        self.assertEqual(payment["amount"], 500.51)
        self.assertEqual(payment["applied_amount"], 500.0)
        self.assertEqual(payment["excess_amount"], 0.51)
        self.assertTrue(payment["requires_admin_review"])
        self.assertEqual(payment["excess_review_status"], "pending")
        self.assertTrue(payment["verification"]["admin_review_required"])
        self.assertEqual(payment["verification"]["status"], "verified")

        self.assertEqual(over["payment"]["amount_paid"], 1000.0)
        self.assertEqual(over["payment"]["payment_status"], "Excess Payment")
        self.assertEqual(over["payment"]["excess_amount"], 0.51)
        self.assertEqual(over["payment"]["balance"], 0.0)
        self.assertEqual(over["payment"]["remaining_balance_exact"], 0.0)
        self.assertTrue(any(
            "EXCESS of PHP 0.51 was held for admin review; only PHP 500.00 was credited." in entry["text"]
            for entry in stored_order(reference)["history"]
        ))

        # The excess is not cash-flow revenue, and another payment cannot be added while it is unresolved.
        summary = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual(summary["received_total"], 1000.0)
        self.admin_post(reference, "payments", {"amount": 1, "method": "Cash", "received": True}, expect=409)

    def test_excess_must_be_resolved_before_completion(self) -> None:
        reference = self.delivered_order(1000)
        over = self.admin_post(reference, "payments", {"amount": 100, "method": "Cash", "received": True})
        payment_number = len(over["payments"])
        order_total = float(over["payment"]["total_amount"])
        self.assertEqual(over["payment"]["payment_status"], "Excess Payment")
        self.admin_post(reference, "status", {"status": "Completed"}, expect=409)

        resolved = self.admin_post(
            reference,
            f"payments/{payment_number}/excess",
            {"resolution": "returned", "note": "Returned the extra PHP 100 at the counter."},
        )
        payment = resolved["payments"][-1]
        self.assertEqual(payment["excess_review_status"], "resolved")
        self.assertEqual(payment["amount"], 100.0)
        self.assertEqual(payment["applied_amount"], 0.0)
        self.assertEqual(resolved["payment"]["amount_paid"], order_total)
        self.assertEqual(resolved["payment"]["payment_status"], "Fully Paid")
        self.assertEqual(self.admin_post(reference, "status", {"status": "Completed"})["status"], "Completed")

    def test_duplicate_payment_reference_is_refused_across_orders(self) -> None:
        shared = workflow.unique_reference()
        first = self.accept(self.customized_order(500))["reference"]
        second = self.accept(self.customized_order(500))["reference"]
        self.submit_payment(first, payment_reference=shared)
        self.submit_payment(second, payment_reference=shared.lower(), expect=409)

    def test_same_receipt_file_cannot_credit_two_orders(self) -> None:
        shared_receipt = {"name": "gcash.png", "dataUrl": PNG}
        first = self.accept(self.customized_order(1000))["reference"]
        second = self.accept(self.customized_order(1000))["reference"]
        self.customer_post(first, "payments", {
            "reference": workflow.unique_reference(), "proof": shared_receipt,
        })
        self.customer_post(second, "payments", {
            "reference": workflow.unique_reference(), "proof": shared_receipt,
        })
        self.assertEqual(len(self.admin_view(first)["payment_submissions"]), 1)
        self.assertEqual(len(self.admin_view(second)["payment_submissions"]), 1)

        verified = self.verify(first)
        self.assertEqual(verified["payment"]["amount_paid"], 500.0)
        second_submission = self.pending_submission(second)
        self.admin_post(second, f"payment-submissions/{second_submission['id']}/verify", {"received": True}, expect=409)
        blocked = self.admin_view(second)
        self.assertEqual(blocked["payments"], [])
        self.assertEqual(blocked["payment"]["amount_paid"], 0.0)
        self.assertEqual(second_submission["status"], "pending")

    def test_refunds_reduce_net_collections_only_when_completed(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.pay_balance(reference)  # 1,000 verified
        refund_id = self.admin_post(reference, "refunds", {"reason": "excess_payment", "details": "Overpaid shipping"})["refunds"][0]["id"]
        self.admin_post(reference, f"refunds/{refund_id}/approve", {"amount": 250})
        self.admin_post(reference, f"refunds/{refund_id}/processing")
        self.admin_post(reference, f"refunds/{refund_id}/fail", {"reason": "Wrong number"})
        failed = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((failed["refunds_completed"], failed["net_collections"], failed["refunds_in_progress"]), (0.0, 1000.0, 250.0))
        self.admin_post(reference, f"refunds/{refund_id}/processing", {"confirmed_not_sent": True})
        self.admin_post(reference, f"refunds/{refund_id}/complete", operations.RefundTests.complete_body(self, **operations.RECIPIENT))
        partial = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((partial["refunds_completed"], partial["net_collections"]), (250.0, 750.0))
        # The payment history is unchanged: refunds are separate records.
        self.assertEqual(sum(item["amount"] for item in stored_order(reference)["payments"]), 1000.0)
        # A second refund can cover only what is left (750), then nothing.
        second = self.admin_post(reference, "refunds", {"reason": "shop_unable", "details": "Cannot finish"})["refunds"][1]["id"]
        self.admin_post(reference, f"refunds/{second}/approve", {"amount": 750.01}, expect=422)
        self.admin_post(reference, f"refunds/{second}/approve", {"amount": 750})
        self.admin_post(reference, f"refunds/{second}/processing")
        self.admin_post(reference, f"refunds/{second}/complete", operations.RefundTests.complete_body(self, **operations.RECIPIENT))
        full = report_services.payment_summary([stored_order(reference)], [], None, None)
        self.assertEqual((full["refunds_completed"], full["net_collections"]), (1000.0, 0.0))
        self.admin_post(reference, "refunds", {"reason": "other", "details": "x"}, expect=409)

    def test_delivery_fee_is_tracked_in_collections_and_historical_reports(self) -> None:
        original = self.settings()
        try:
            settings = json.loads(json.dumps(original))
            for area in settings["areas"]:
                if area["region"] == "visayas":
                    area["fee"] = 200
            self.save_settings(settings)
            reference = self.delivered_order(1000, fulfillment=operations.CEBU)
        finally:
            self.save_settings(original)
        order = stored_order(reference)
        self.assertEqual(order["total_amount"], 1200.0)
        summary = report_services.payment_summary([order], [], None, None)
        self.assertEqual((summary["verified_collections"], summary["delivery_fee_collections"]), (1200.0, 200.0))
        self.admin_post(reference, "status", {"status": "Completed"})
        today = report_services.today_local().isoformat()
        report = self.admin.get("/api/admin/reports/summary", params={"preset": "custom", "date_from": today, "date_to": today, "service": "souvenir"}).json()
        tx = [row for row in report["revenue_transactions"] if row["reference"] == reference]
        self.assertEqual(tx[0]["confirmed_revenue"], 1200.0)
        # Starting a reprint does not change the historical report.
        self.admin_post(reference, "reprint", {"reason": "Cracked mug", "authorized": True})
        again = self.admin.get("/api/admin/reports/summary", params={"preset": "custom", "date_from": today, "date_to": today, "service": "souvenir"}).json()
        self.assertEqual(again["totals"]["confirmed_revenue"], report["totals"]["confirmed_revenue"])


# ====================================================================== database integrity


MIGRATION_SCRIPT = r"""
import json, os, sqlite3, sys
db = sys.argv[1]
connection = sqlite3.connect(db)
# The orders table as it was before production, drafts, cancellation_requests, and refunds existed.
connection.execute('''CREATE TABLE orders (
    reference TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, customer TEXT NOT NULL,
    product TEXT NOT NULL, customization TEXT NOT NULL, event_details TEXT NOT NULL, has_event INTEGER NOT NULL,
    acknowledged INTEGER NOT NULL, status TEXT NOT NULL, total_amount REAL, payment_instructions TEXT NOT NULL DEFAULT '',
    payments TEXT NOT NULL DEFAULT '[]', attachments TEXT NOT NULL DEFAULT '[]', draft_attachment TEXT,
    history TEXT NOT NULL DEFAULT '[]', admin_notes TEXT NOT NULL DEFAULT '', customer_id TEXT, idempotency_key TEXT,
    pricing TEXT NOT NULL DEFAULT '{}', completed_at TEXT, payment_option TEXT NOT NULL DEFAULT 'down_payment',
    fulfillment TEXT NOT NULL DEFAULT '{}', quotation TEXT NOT NULL DEFAULT '{}', payment_submissions TEXT NOT NULL DEFAULT '[]',
    quotation_history TEXT NOT NULL DEFAULT '[]')''')
connection.execute(
    "INSERT INTO orders (reference, created_at, updated_at, customer, product, customization, event_details, has_event, acknowledged, status, total_amount, payments, history, fulfillment) "
    "VALUES ('OLD-10-01-26', '2026-10-01T00:00:00+00:00', '2026-10-02T00:00:00+00:00', ?, ?, ?, '{}', 0, 1, 'Out for Delivery', 500, ?, ?, ?)",
    (json.dumps({"name": "Old", "email": "old@example.test", "phone": "", "messenger": ""}),
     json.dumps({"id": "white-mug", "name": "White Mug", "category": "Souvenir", "description": "", "image": ""}),
     json.dumps({"quantity": 1}),
     json.dumps([{"amount": 500, "method": "Cash", "reference": "OR-OLD-1", "received_at": "2026-10-02T00:00:00+00:00"}]),
     json.dumps([{"at": "2026-10-01T00:00:00+00:00", "text": "Customer submitted an order request."}]),
     json.dumps({"method": "delivery", "recipient_name": "Old", "province": "Cebu"})))
connection.commit()
connection.close()
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.repositories import order_repository
with TestClient(app):
    order = order_repository.get_order("OLD-10-01-26")
    columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(orders)")}
    tables = {row[0] for row in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print(json.dumps({
        "columns": sorted(columns & {"production", "drafts", "cancellation_requests", "refunds"}),
        "has_payment_methods": "payment_methods" in tables,
        "status": order["status"], "payments": order["payments"], "production": order["production"], "refunds": order["refunds"],
        "fulfillment": order["fulfillment"],
    }))
"""


class DatabaseIntegrityTests(AuditTestCase):
    def test_existing_database_is_migrated_without_losing_records(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            # A local SQLite file (the Turso path is checked separately with the same old schema).
            env = {key: value for key, value in os.environ.items() if not key.startswith("TURSO_")}
            env.update({"PRINTSHOP_DATABASE_PATH": str(Path(folder) / "old.sqlite3"), "PRINTSHOP_UPLOAD_DIR": str(Path(folder) / "up"), "PRINTSHOP_ENV_FILE": ""})
            result = subprocess.run([sys.executable, "-c", MIGRATION_SCRIPT, env["PRINTSHOP_DATABASE_PATH"]], cwd=PROJECT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["columns"], ["cancellation_requests", "drafts", "production", "refunds"])
        self.assertTrue(data["has_payment_methods"])
        self.assertEqual((data["status"], data["payments"][0]["reference"], data["production"], data["refunds"]), ("Out for Delivery", "OR-OLD-1", {}, []))
        self.assertEqual(data["fulfillment"]["recipient_name"], "Old")

    def test_backup_restores_production_refunds_and_payment_methods(self) -> None:
        reference, refund_id = operations.RefundTests.cancelled_with_refund(self, 600)
        backup = self.admin.post("/api/admin/backup")
        self.assertEqual(backup.status_code, 200, backup.text)
        document = backup.json()
        self.assertIn("payment_methods", document["tables"])
        # Change the data, then restore the backup: the refund returns to its saved state.
        self.admin_post(reference, f"refunds/{refund_id}/processing")
        restored = self.admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": document})
        self.assertEqual(restored.status_code, 200, restored.text)
        order = stored_order(reference)
        self.assertEqual((order["status"], order["refunds"][0]["status"], order["refunds"][0]["amount"]), ("Cancelled", "approved", 300.0))
        self.assertEqual(order["cancellation_requests"][0]["status"], "approved")
        self.assertTrue(any(item["provider"] == "GCash" for item in self.client.get("/api/payment-methods").json()["methods"]))

    def test_concurrent_actions_on_the_same_version_apply_once(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        self.through_draft_approval(reference)
        self.admin_post(reference, "status", {"status": "In Production"})
        version = self.admin_view(reference)["version"]
        codes: list[int] = []

        def update(progress: int) -> None:
            client = TestClient(app)
            client.cookies.set(workflow.SESSION_COOKIE, workflow.create_session_token(workflow.ADMIN_EMAIL, "admin"))
            codes.append(client.post(f"/api/admin/orders/{reference}/production", json={"progress": progress, "expected_version": version}).status_code)

        threads = [threading.Thread(target=update, args=(value,)) for value in (30, 40, 50, 60)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(codes), [200, 409, 409, 409])
        self.assertEqual(len(stored_order(reference)["production"]["updates"]), 1)

    def test_failed_save_leaves_no_partial_change(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        before = stored_order(reference)
        # A stale version is refused before anything is written (no history entry, no audit row).
        self.admin_post(reference, "status", {"status": "Draft Preparation", "expected_version": before and len(before["history"]) - 1}, expect=409)
        after = stored_order(reference)
        self.assertEqual((after["status"], len(after["history"])), (before["status"], len(before["history"])))


# ====================================================================== security


class SecurityTests(AuditTestCase):
    def test_other_customers_cannot_read_or_change_an_order(self) -> None:
        reference = self.delivered_order()
        stranger, stranger_email = self.new_customer()
        file_url = self.shopper.get(f"/api/account/orders/{reference}").json()["fulfillment"]["attempts"][0]["proof"][0]["url"]
        self.assertEqual(self.shopper.get(file_url).status_code, 200)
        self.assertEqual(stranger.get(file_url).status_code, 404)
        self.assertEqual(stranger.get(f"/api/account/orders/{reference}").status_code, 404)
        for action, body in (
            ("cancellation-request", {"reason": "other", "remarks": "x", "acknowledged": True}),
            ("refund-request", {"reason": "damaged_product", "details": "x"}),
            ("delivery-issue", {"type": "late", "details": "x"}),
            ("payments", {"reference": workflow.unique_reference(), "proof": {"name": "a.png", "dataUrl": PNG}}),
        ):
            self.assertEqual(stranger.post(f"/api/orders/{reference}/{action}", json=body).status_code, 404, action)
        anonymous = TestClient(app)
        self.assertEqual(anonymous.post(f"/api/orders/{reference}/refund-request", json={"reason": "other", "details": "x"}).status_code, 401)

    def test_customers_cannot_use_administrator_endpoints(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        submission = self.submit_payment(reference)["payment_submissions"][-1]["id"]
        attempts = [
            ("post", f"/api/admin/orders/{reference}/payment-submissions/{submission}/verify", {"received": True}),
            ("post", f"/api/admin/orders/{reference}/production", {"progress": 10}),
            ("post", f"/api/admin/orders/{reference}/quality-check", {"checks": operations.CHECKS_PASSED}),
            ("post", f"/api/admin/orders/{reference}/dispatch", {"tracking_number": "JT0000000000"}),
            ("post", f"/api/admin/orders/{reference}/pickup/confirm", {}),
            ("post", f"/api/admin/orders/{reference}/reprint", {"reason": "x", "authorized": True}),
            ("post", f"/api/admin/orders/{reference}/refunds", {"reason": "other", "details": "x"}),
            ("get", "/api/admin/settings/payment-methods", None),
            ("post", "/api/admin/settings/fulfillment", {}),
            ("get", "/api/admin/refunds", None),
            ("get", "/api/admin/production", None),
        ]
        for method, url, body in attempts:
            response = getattr(self.shopper, method)(url, **({"json": body} if body is not None else {}))
            self.assertIn(response.status_code, {401, 403}, url)
        # The submission is still waiting: the customer could not verify their own payment.
        self.assertEqual(self.pending_submission(reference)["status"], "pending")

    def test_cross_site_requests_are_refused(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        response = self.shopper.post(f"/api/orders/{reference}/cancellation-request", json={"reason": "other"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403, response.text)
        response = self.admin.post(f"/api/admin/orders/{reference}/production", json={"progress": 10}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403, response.text)

    def test_status_endpoint_cannot_skip_workflow_steps(self) -> None:
        reference = self.confirmed_order(1000)["reference"]
        for status in ("Packaging", "Awaiting Full Payment", "Ready for Dispatch", "Delivered", "Order Claimed", "Completed", "Confirmed", "Made Up"):
            response = self.admin.post(f"/api/admin/orders/{reference}/status", json={"status": status, "expected_version": self.admin_view(reference)["version"]})
            self.assertIn(response.status_code, {409, 422}, status)
        self.admin_post(reference, "dispatch", {"tracking_number": "JT1234567890"}, expect=409)
        self.admin_post(reference, "pickup/confirm", {"claimant_name": "x", "claimant_type": "customer", "verification_code": reference, "acknowledged": True}, expect=409)
        self.admin_post(reference, "delivery/delivered", {"proof": operations.proof()}, expect=409)

    def test_uploads_are_type_checked_everywhere(self) -> None:
        import base64

        fake_png = {"name": "x.png", "dataUrl": "data:image/png;base64," + base64.b64encode(b"MZ\x90\x00 not an image").decode()}
        svg = {"name": "x.svg", "dataUrl": "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode()}
        reference = self.confirmed_order(1000)["reference"]
        self.through_draft_approval(reference)
        self.admin_post(reference, "status", {"status": "In Production"})
        for bad in (fake_png, svg):
            self.admin_post(reference, "production", {"progress": 5, "photos": [bad]}, expect=422)
        method = next(item for item in self.admin.get("/api/admin/settings/payment-methods").json()["methods"] if item["provider"] == "GCash")
        self.assertEqual(self.admin.patch(f"/api/admin/settings/payment-methods/{method['id']}", json={"qr": svg}).status_code, 422)
        pdf = {"name": "qr.pdf", "dataUrl": "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4\n%\xe2\xe3").decode()}
        self.assertEqual(self.admin.patch(f"/api/admin/settings/payment-methods/{method['id']}", json={"qr": pdf}).status_code, 422)
        self.assertEqual(self.client.get("/api/payment-methods/pm_doesnotexist/qr").status_code, 404)


if __name__ == "__main__":
    unittest.main()
