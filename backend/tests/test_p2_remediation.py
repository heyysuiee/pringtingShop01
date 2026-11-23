"""Regression tests for the derived P2 remediation.

Frontend findings are covered by static source checks (the UI has no JS unit-test harness in this
repository). Backend and deployment findings use temporary SQLite files and synthetic data; no live
provider or production data is used.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock


_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="impress-p2-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_TEST_DIRECTORY.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_TEST_DIRECTORY.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")
os.environ.setdefault("PRINTSHOP_ADMIN_EMAIL", "admin@example.test")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as main_module  # noqa: E402
from backend.app.core.config import settings  # noqa: E402
from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.repositories import (  # noqa: E402
    admin_repository,
    customer_repository,
    file_repository,
)
from backend.app.services import file_services  # noqa: E402
from backend.app.utils.clock import now_iso, now_utc  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


class P2FrontendSourceTests(unittest.TestCase):
    def test_shared_phone_validation_is_loaded_and_used(self) -> None:
        phone = _read("frontend/assets/phone.js")
        self.assertIn("window.PrintShopPhone = { valid };", phone)
        self.assertIn(r"^(?:0|63)9\d{9}$", phone)
        self.assertIn(r"^0\d{7,9}$", phone)
        for page in ("order.html", "create-account.html", "customer-portal.html"):
            self.assertIn('/assets/phone.js', _read(f"frontend/customer/{page}"))
        self.assertIn("window.PrintShopPhone?.valid", _read("frontend/assets/order.js"))
        self.assertIn("window.PrintShopPhone?.valid", _read("frontend/assets/auth.js"))
        self.assertIn("window.PrintShopPhone?.valid", _read("frontend/assets/account.js"))

    def test_delivery_location_selects_are_populated_and_cleared(self) -> None:
        order = _read("frontend/assets/order.js")
        self.assertIn("province?.cities", order)
        self.assertIn('areaSelect.value = ""', order)
        self.assertIn("syncProvinceSelection()", order)

    def test_date_minima_use_philippine_time(self) -> None:
        dynamic_form = _read("frontend/assets/dynamic-form.js")
        self.assertIn('timeZone: "Asia/Manila"', dynamic_form)
        self.assertIn("control.min = manilaToday()", dynamic_form)
        self.assertIn("manilaToday };", dynamic_form)
        self.assertIn("DynamicForm.manilaToday()", _read("frontend/assets/order.js"))

    def test_backup_restore_metadata_is_labelled_as_unverified_claims(self) -> None:
        dashboard = _read("frontend/assets/admin/dashboard.js")
        self.assertIn("let selectionToken = 0;", dashboard)
        self.assertIn("if (token !== selectionToken) return;", dashboard)
        self.assertIn("file claims", dashboard)
        self.assertIn("Claimed creation time", dashboard)

    def test_payment_method_load_errors_are_visible_and_blocking(self) -> None:
        portal = _read("frontend/assets/portal.js")
        self.assertIn("function paymentMethodsStatus", portal)
        self.assertIn("Payment methods could not be loaded", portal)
        self.assertNotIn("const hasReceipt", portal)
        tracking = _read("frontend/assets/portal-tracking.js")
        self.assertIn("data-methods-error", tracking)
        self.assertIn("Retry payment methods", tracking)
        self.assertIn("context.confirmAction", tracking)

    def test_confirmation_dialogs_cannot_stack_or_reuse_ids(self) -> None:
        confirm = _read("frontend/assets/confirm-dialog.js")
        self.assertIn("let pending = null;", confirm)
        self.assertIn("if (pending) return pending;", confirm)
        self.assertIn("theme-confirm-title-${", confirm)
        self.assertIn("confirm-input-${", _read("frontend/assets/admin/admin-core.js"))

    def test_hardcoded_provider_copy_was_removed_from_customer_flows(self) -> None:
        dashboard = _read("frontend/assets/admin/dashboard.js")
        operations = _read("frontend/assets/admin/operations-admin.js")
        self.assertNotIn("through GCash", dashboard)
        self.assertNotIn('"GCash, Maya, and other e-wallets', operations)
        self.assertIn("configured e-wallet(s)", dashboard)


class P2BackendSourceTests(unittest.TestCase):
    def test_backup_takes_the_write_lock_before_scanning_disk(self) -> None:
        source = _read("backend/app/services/backup_services.py")
        create_backup = source[source.index("def create_backup"):]
        self.assertLess(
            create_backup.index('connection.execute("BEGIN IMMEDIATE")'),
            create_backup.index("files = _read_disk_files()"),
        )

    def test_disk_uploads_are_published_atomically(self) -> None:
        source = _read("backend/app/services/file_services.py")
        self.assertIn("def _atomic_write", source)
        self.assertIn("os.replace(temporary, target)", source)
        self.assertIn("def _expired", source)
        self.assertIn("if _expired(session):", source)

    def test_inline_body_cap_covers_every_file_bearing_route_and_streams_bytes(self) -> None:
        source = _read("backend/app/main.py")
        for path in ("/refund-request$", "/delivery-issue$", "/refunds/[^/]+/[^/]+$"):
            self.assertIn(path, source)
        self.assertIn("len(body) > INLINE_BODY_LIMIT", source)
        self.assertIn('"GET", "POST", "PUT", "PATCH", "DELETE"', source)

    def test_runtime_dependency_lists_cover_gateway_imports(self) -> None:
        gateway = _read("backend/app/services/payment_verification_services.py")
        self.assertIn("import httpx", gateway)
        for requirements in ("requirements.txt", "backend/app/requirements.txt"):
            self.assertIn("httpx>=", _read(requirements))

    def test_atomic_write_publishes_complete_bytes_without_temp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "proof.png"
            file_services._atomic_write(target, iter([b"abc", b"def"]))
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(list(Path(directory).iterdir()), [target])


class P2RuntimeBehaviourTests(unittest.TestCase):
    _STATE_TABLES = ("customer_accounts", "customer_password_resets", "admin_credentials", "admin_password_resets")

    def setUp(self) -> None:
        file_repository.initialize()
        customer_repository.initialize()
        admin_repository.initialize()
        # Reset tests raise session versions and remove credentials; restore the exact pre-test state so
        # admin sessions issued by other test modules stay valid when the full suite runs in one process.
        self._state: dict[str, tuple[list[str], list[tuple[object, ...]]]] = {}
        with connection_scope() as connection:
            for table in self._STATE_TABLES:
                cursor = connection.execute(f"SELECT * FROM {table}")
                columns = [description[0] for description in cursor.description]
                self._state[table] = (columns, [tuple(row) for row in cursor.fetchall()])

    def tearDown(self) -> None:
        with connection_scope() as connection:
            for table in reversed(self._STATE_TABLES):  # children before parents
                connection.execute(f"DELETE FROM {table}")
            for table in self._STATE_TABLES:  # parents before children
                columns, rows = self._state[table]
                if rows:
                    placeholders = ", ".join("?" for _ in columns)
                    connection.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        rows,
                    )

    def test_customer_reset_token_is_consumed_only_once(self) -> None:
        email = f"reset-{threading.get_ident()}-{os.getpid()}@example.test"
        customer = customer_repository.create(
            {"email": email, "name": "Reset Test", "phone": "09175551234", "password_hash": hash_password("Old-Pass-123!")},
            now_iso(),
        )
        token_hash = "c" * 64
        expires = (now_utc() + timedelta(minutes=10)).isoformat(timespec="seconds")
        customer_repository.replace_reset_code(customer["id"], "code-hash", expires, now_iso())
        customer_repository.set_reset_token(customer["id"], token_hash, expires)

        first = customer_repository.consume_password_reset(
            customer["id"], token_hash, hash_password("New-Pass-123!"), now_iso(), lambda _: False
        )
        second = customer_repository.consume_password_reset(
            customer["id"], token_hash, hash_password("Other-Pass-123!"), now_iso(), lambda _: False
        )
        self.assertEqual(first, "ok")
        self.assertEqual(second, "invalid")
        self.assertIsNone(customer_repository.get_reset(customer["id"]))

    def test_admin_reset_token_is_consumed_only_once(self) -> None:
        token_hash = "a" * 64
        expires = (now_utc() + timedelta(minutes=10)).isoformat(timespec="seconds")
        admin_repository.replace_reset_code("code-hash", expires, now_iso())
        admin_repository.set_reset_token(token_hash, expires)

        first = admin_repository.consume_password_reset(
            token_hash, hash_password("New-Admin-Pass-123!"), now_iso(), "", lambda _: False
        )
        second = admin_repository.consume_password_reset(
            token_hash, hash_password("Other-Admin-Pass-123!"), now_iso(), "", lambda _: False
        )
        self.assertEqual(first, "ok")
        self.assertEqual(second, "invalid")
        self.assertIsNone(admin_repository.get_reset())

    def test_expired_upload_session_is_rejected_and_removed(self) -> None:
        session_id = "upl_p2_expired"
        old = (now_utc() - timedelta(hours=25)).isoformat(timespec="seconds")
        with connection_scope() as connection:
            connection.execute(
                "INSERT INTO upload_sessions (id, customer_id, product_id, field_key, name, extension, mime_type, size, created_at)"
                " VALUES (?, ?, ?, '', 'old.png', '.png', 'image/png', 4, ?)",
                (session_id, "customer-1", "product-1", old),
            )
        with self.assertRaises(HTTPException) as failure:
            file_services._owned_session("customer-1", session_id)
        self.assertEqual(failure.exception.status_code, 404)
        self.assertIsNone(file_repository.get_session(session_id))

    def test_inline_body_cap_applies_without_content_length(self) -> None:
        with mock.patch.object(main_module, "INLINE_BODY_LIMIT", 64):
            with TestClient(main_module.app) as client:
                declared = client.post("/api/orders/ref/payments", content=b"x" * 100)
                self.assertEqual(declared.status_code, 413)

                def streamed():
                    yield b"x" * 50
                    yield b"y" * 50

                chunked = client.post("/api/orders/ref/payments", content=streamed())
                self.assertEqual(chunked.status_code, 413)


class P2ProductionConfigTests(unittest.TestCase):
    def _production_settings(self, **changes: object) -> object:
        production = {
            "environment": "production",
            "secret_key": "s" * 48,
            "backup_signing_key": "b" * 48,
            "admin_email": "admin@example.test",
            "admin_password_hash": "pbkdf2_sha256$test",
            "allowed_hosts": ("shop.example.test",),
            "require_email_verification": False,
            "file_storage": "database",
        }
        production.update(changes)
        return replace(settings, **production)

    def _environment(self, **changes: str) -> dict[str, str]:
        environment = {
            "PRINTSHOP_SECRET_KEY": "s" * 48,
            "PRINTSHOP_DATABASE_PATH": str(ROOT / "backend" / "data" / "p2-validate.sqlite3"),
            "PRINTSHOP_UPLOAD_DIR": str(ROOT / "backend" / "data" / "p2-validate-uploads"),
            "PRINTSHOP_ALLOWED_HOSTS": "shop.example.test",
            "RENDER": "",
        }
        environment.update(changes)
        return environment

    def _assert_rejected(self, expected: str, settings_changes: dict[str, object] | None = None, env_changes: dict[str, str] | None = None) -> None:
        with mock.patch.dict(os.environ, self._environment(**(env_changes or {})), clear=False):
            with self.assertRaises(RuntimeError) as failure:
                self._production_settings(**(settings_changes or {})).validate()
        self.assertIn(expected, str(failure.exception))

    def test_production_missing_secret_is_rejected(self) -> None:
        self._assert_rejected("missing", env_changes={"PRINTSHOP_SECRET_KEY": ""})

    def test_production_short_secret_is_rejected(self) -> None:
        self._assert_rejected(
            "at least 32",
            settings_changes={"secret_key": "short"},
            env_changes={"PRINTSHOP_SECRET_KEY": "short"},
        )

    def test_production_wildcard_host_is_rejected(self) -> None:
        self._assert_rejected("explicit host names", settings_changes={"allowed_hosts": ("*",)})

    def test_production_bad_admin_hash_is_rejected(self) -> None:
        self._assert_rejected("PBKDF2", settings_changes={"admin_password_hash": "plaintext"})

    def test_email_verification_requires_smtp_in_production(self) -> None:
        self._assert_rejected(
            "Email verification requires",
            settings_changes={"require_email_verification": True, "smtp_host": "", "smtp_from": ""},
        )

    def test_render_requires_turso(self) -> None:
        self._assert_rejected("Render deployments require", env_changes={"RENDER": "1"})

    def test_short_previous_backup_key_is_rejected(self) -> None:
        self._assert_rejected(
            "PREVIOUS_SIGNING_KEYS",
            settings_changes={"previous_backup_signing_keys": ("short",)},
        )


class P2DeploymentDocumentationTests(unittest.TestCase):
    def test_preview_runbook_can_start_without_smtp(self) -> None:
        runbook = _read("docs/vercel-preview-deployment-runbook.md")
        self.assertIn("PRINTSHOP_REQUIRE_EMAIL_VERIFICATION=false", runbook)

    def test_render_blueprint_requires_turso_and_backup_keys(self) -> None:
        render = _read("render.yaml")
        self.assertIn("TURSO_DATABASE_URL", render)
        self.assertIn("refuses to start on Render without them", render)
        self.assertIn("PRINTSHOP_BACKUP_SIGNING_KEY", render)
        self.assertIn("PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS", render)

    def test_container_defaults_do_not_trust_proxy_headers_or_leak_env_files(self) -> None:
        self.assertNotIn("--proxy-headers", _read("Dockerfile"))
        dockerignore = _read(".dockerignore")
        self.assertIn(".env*", dockerignore)
        self.assertIn("backend/app/.env*", dockerignore)
        self.assertIn(".vercel", dockerignore)

    def test_environment_documentation_and_test_counts_are_current(self) -> None:
        accounts = _read("docs/customer-accounts.md")
        for variable in ("PRINTSHOP_ENV_FILE", "PRINTSHOP_ORDER_DB", "GOOGLE_API_KEY", "PRINTSHOP_BACKUP_SIGNING_KEY"):
            self.assertIn(variable, accounts)
        self.assertIn("at least 8 characters", accounts)
        self.assertIn("shared database store", accounts)
        implementation = _read("docs/implementation-report.md")
        self.assertNotIn("276 automated tests", implementation)
        self.assertIn("email verification is explicitly disabled", implementation)
        self.assertIn("current consolidated result", implementation)
        self.assertIn(
            "Final run: `python -m unittest discover -s backend/tests` → **403 tests, OK**",
            _read("docs/ocr-live-deployment-checklist.md"),
        )
        self.assertIn("only the English Tesseract language pack", _read(".env.example"))
        report = _read("docs/qa-remediation-report.md")
        self.assertIn("A live Vercel preview against a copied Turso database", report)
        self.assertIn("production deployment remains BLOCKED", report)


if __name__ == "__main__":
    unittest.main()
