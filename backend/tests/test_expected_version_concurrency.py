"""Optimistic concurrency: every admin order action must carry a valid, current version.

A missing or malformed ``expected_version`` used to fall back to the version read by the request,
which let a stale screen apply an action to newer state. The HTTP boundary now refuses it before a
service runs, and the service/repository guards stay as the last line of defence.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-version-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402

from backend.app.repositories import order_repository  # noqa: E402
from backend.app.services import order_services  # noqa: E402
from backend.tests import test_order_workflow as workflow  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


class ExpectedVersionTests(workflow.WorkflowTestCase):
    def test_admin_order_actions_require_a_positive_whole_version(self) -> None:
        reference = self.place()["reference"]
        version = self.admin_view(reference)["version"]
        url = f"/api/admin/orders/{reference}/status"
        for body in (
            {"status": "Under Review"},
            {"status": "Under Review", "expected_version": None},
            {"status": "Under Review", "expected_version": "1"},
            {"status": "Under Review", "expected_version": True},
            {"status": "Under Review", "expected_version": 1.0},
            {"status": "Under Review", "expected_version": 0},
            {"status": "Under Review", "expected_version": -1},
        ):
            response = self.admin.post(url, json=body)
            self.assertEqual(response.status_code, 422, response.text)

        self.assertEqual(self.admin_view(reference)["version"], version)
        accepted = self.admin.post(url, json={"status": "Under Review", "expected_version": version})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.admin_view(reference)["version"], version + 1)

    def test_raw_body_admin_actions_require_a_positive_whole_version(self) -> None:
        reference = self.confirmed_order()["reference"]
        url = f"/api/admin/orders/{reference}/refunds"
        for body in (
            {"reason": "excess_payment", "details": "Overpaid"},
            {"reason": "excess_payment", "details": "Overpaid", "expected_version": "1"},
            {"reason": "excess_payment", "details": "Overpaid", "expected_version": True},
        ):
            response = self.admin.post(url, json=body)
            self.assertEqual(response.status_code, 422, response.text)

        version = self.admin_view(reference)["version"]
        accepted = self.admin.post(url, json={
            "reason": "excess_payment", "details": "Overpaid", "expected_version": version,
        })
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_stale_version_is_still_refused_without_a_partial_write(self) -> None:
        reference = self.place()["reference"]
        first = self.admin_post(reference, "status", {"status": "Under Review"})
        stale = first["version"] - 1
        response = self.admin.post(f"/api/admin/orders/{reference}/status", json={
            "status": "Awaiting Quotation", "expected_version": stale,
        })
        self.assertEqual(response.status_code, 409, response.text)
        after = stored_order(reference)
        self.assertEqual((after["status"], len(after["history"])), (first["status"], first["version"]))

    def test_service_and_repository_reject_malformed_versions(self) -> None:
        reference = self.place()["reference"]
        before = stored_order(reference)
        with self.assertRaises(HTTPException) as raised:
            order_services.update_order_status(reference, "Under Review", {"expected_version": True})
        self.assertEqual(raised.exception.status_code, 422)

        with self.assertRaises(order_repository.StaleOrderError):
            order_repository.update_order(
                reference, {"status": "Under Review"}, "invalid version", expected_version=True,
            )
        self.assertEqual(stored_order(reference)["status"], before["status"])


if __name__ == "__main__":
    unittest.main()
