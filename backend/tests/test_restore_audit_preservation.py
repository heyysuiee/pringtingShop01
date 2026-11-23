"""Restore must never erase the audit trail.

Restoring an older backup rolls the shop's data back, but the append-only audit log keeps every entry —
including entries created after the backup and entries about orders the restore removes — and records the
restore itself, so a rollback can never hide payment or refund evidence.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-restore-audit-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.repositories import audit_repository, order_repository  # noqa: E402
from backend.app.services import backup_services, order_services  # noqa: E402
from backend.tests import test_admin_improvements as admin_tests  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


def audit_ids(reference: str) -> set[str]:
    return {entry["id"] for entry in audit_repository.list_for_order(reference)}


def audit_count() -> int:
    with connection_scope() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM order_audit_log").fetchone()[0])


def restore_events() -> list[dict]:
    """The shop-wide restore entries ("*" marks an entry that belongs to no single order)."""
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM order_audit_log WHERE order_reference = '*' ORDER BY at, rowid"
        ).fetchall()
    return [
        {"actor_id": row["actor_id"], "action": row["action"], "details": json.loads(row["details"] or "{}")}
        for row in rows
    ]


class RestoreAuditTests(admin_tests.AdminTestCase):
    def restore(self, admin, backup: dict) -> dict:
        response = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": backup})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_entries_created_after_the_backup_survive_the_restore(self) -> None:
        admin = admin_tests.admin_client()
        reference = admin_tests.make_order(admin_tests.make_customer(), status="Pending Confirmation", total=None)
        order_services.update_order_status(reference, "Under Review")
        backup = admin.post("/api/admin/backup").json()
        before = audit_ids(reference)
        self.assertTrue(before)
        # An audited action after the backup: restoring must roll the data back without erasing this.
        order_services.update_order_status(reference, "Awaiting Quotation")
        after = audit_ids(reference)
        self.assertTrue(before < after)

        result = self.restore(admin, backup)
        self.assertEqual(stored_order(reference)["status"], "Under Review")
        self.assertTrue(after <= audit_ids(reference))
        # Every backup entry was already present, so nothing needed re-inserting.
        self.assertEqual(result["restored"]["order_audit_log"], 0)

        events = restore_events()
        self.assertEqual(events[-1]["action"], "backup.restored")
        self.assertEqual(events[-1]["actor_id"], settings.admin_email or "admin@example.test")
        self.assertEqual(events[-1]["details"]["backup_created_at"], backup["created_at"])
        self.assertEqual(events[-1]["details"]["backup_created_by"], backup["created_by"])

    def test_audit_of_orders_removed_by_the_restore_is_kept(self) -> None:
        admin = admin_tests.admin_client()
        backup = admin.post("/api/admin/backup").json()
        later = admin_tests.make_order(admin_tests.make_customer("Later Order"), status="Pending Confirmation", total=None)
        order_services.update_order_status(later, "Under Review")
        later_audit = audit_ids(later)
        self.assertTrue(later_audit)

        self.restore(admin, backup)
        self.assertIsNone(order_repository.get_order(later))
        # The order is gone, but what happened to it is still on the record.
        self.assertTrue(later_audit <= audit_ids(later))

    def test_restoring_twice_never_duplicates_entries(self) -> None:
        admin = admin_tests.admin_client()
        reference = admin_tests.make_order(admin_tests.make_customer(), status="Pending Confirmation", total=None)
        order_services.update_order_status(reference, "Under Review")
        backup = admin.post("/api/admin/backup").json()

        events_before = len(restore_events())
        self.restore(admin, backup)
        first_ids = audit_ids(reference)
        first_count = audit_count()
        self.assertEqual(len(restore_events()), events_before + 1)
        self.restore(admin, backup)
        self.assertEqual(audit_ids(reference), first_ids)
        # Only the second restore's own entry is added; no backup entry is written twice.
        self.assertEqual(audit_count(), first_count + 1)
        self.assertEqual(len(restore_events()), events_before + 2)

    def test_restore_brings_back_entries_missing_from_this_database(self) -> None:
        admin = admin_tests.admin_client()
        reference = admin_tests.make_order(admin_tests.make_customer(), status="Pending Confirmation", total=None)
        order_services.update_order_status(reference, "Under Review")
        backup = admin.post("/api/admin/backup").json()
        backed_up = audit_ids(reference)
        # Simulate a fresh installation whose trail does not have this order's entries yet.
        with connection_scope() as connection:
            connection.execute("DELETE FROM order_audit_log WHERE order_reference = ?", (reference,))

        result = self.restore(admin, backup)
        self.assertEqual(result["restored"]["order_audit_log"], len(backed_up))
        self.assertTrue(backed_up <= audit_ids(reference))

    def test_failed_restore_changes_no_audit_entry(self) -> None:
        admin = admin_tests.admin_client()
        reference = admin_tests.make_order(admin_tests.make_customer(), status="Pending Confirmation", total=None)
        order_services.update_order_status(reference, "Under Review")
        backup = admin.post("/api/admin/backup").json()
        before_ids = audit_ids(reference)
        before_count = audit_count()
        events_before = len(restore_events())
        tampered = copy.deepcopy(backup)
        tampered["signature"] = "0" * 64
        response = admin.post("/api/admin/backup/restore", json={"confirmation": "RESTORE", "backup": tampered})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(audit_ids(reference), before_ids)
        self.assertEqual(audit_count(), before_count)
        self.assertEqual(len(restore_events()), events_before)

    def test_backup_without_audit_ids_is_refused(self) -> None:
        admin = admin_tests.admin_client()
        backup = admin.post("/api/admin/backup").json()
        broken = copy.deepcopy(backup)
        columns = broken["tables"]["order_audit_log"]["columns"]
        position = columns.index("id")
        broken["tables"]["order_audit_log"]["columns"] = [name for name in columns if name != "id"]
        broken["tables"]["order_audit_log"]["rows"] = [
            [value for index, value in enumerate(row) if index != position]
            for row in broken["tables"]["order_audit_log"]["rows"]
        ]
        # Re-sign so the file passes the integrity checks and reaches the table validation itself.
        broken["checksum"] = backup_services._checksum(broken["tables"], broken.get("files", {}), broken["version"])
        broken["signature"] = backup_services._signature(broken["checksum"], broken["created_at"], broken["version"])
        with self.assertRaises(HTTPException) as raised:
            backup_services.validate_backup(broken)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("id", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
