"""Regression tests for the P1 audit remediation.

Everything uses a temporary SQLite database and synthetic data; no external services are contacted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


_test_directory = tempfile.TemporaryDirectory(prefix="impress-p1-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")
os.environ.setdefault("PRINTSHOP_ADMIN_EMAIL", "admin@example.test")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.config import settings  # noqa: E402
from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.database.connection import connection_scope, exclusive_write  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import (  # noqa: E402
    admin_repository,
    auth_throttle_repository,
    customer_repository,
    file_repository,
)
from backend.app.services import (  # noqa: E402
    admin_auth_services,
    auth_services,
    backup_services,
    file_services,
    location_services,
    payment_verification_services as gateway,
    shop_settings_services,
)
from backend.app.routes import auth as auth_routes  # noqa: E402
from backend.app.services import email_service  # noqa: E402

from backend.app.utils.clock import now_utc  # noqa: E402


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class AtomicOtpTests(unittest.TestCase):
    def setUp(self) -> None:
        customer_repository.initialize()
        admin_repository.initialize()

    def _new_customer(self) -> str:
        with TestClient(app) as client:
            email = f"otp-{uuid.uuid4().hex[:10]}@example.test"
            body = {"email": email, "password": "Audit-Pass-123!", "name": "OTP Test", "phone": "09175551234"}
            self.assertEqual(client.post("/api/customer/register", json=body).status_code, 201)
            return client.get("/api/customer/session").json()["customer"]["id"]

    def _issue_code(self, customer_id: str, code: str) -> None:
        digest = auth_services._keyed_digest("otp", customer_id, code)
        customer_repository.replace_reset_code(
            customer_id, digest, (now_utc() + timedelta(minutes=5)).isoformat(timespec="seconds"), now_utc().isoformat(timespec="seconds")
        )

    def test_customer_concurrent_guesses_cannot_bypass_attempt_limit(self) -> None:
        customer_id = self._new_customer()
        self._issue_code(customer_id, "123456")
        wrong = auth_services._keyed_digest("otp", customer_id, "000000")
        token_hash = auth_services._keyed_digest("reset", customer_id, "unused")
        expires = (now_utc() + timedelta(minutes=10)).isoformat(timespec="seconds")
        results: list[str] = []
        barrier = threading.Barrier(12)

        def guess() -> None:
            barrier.wait()
            try:
                outcome = customer_repository.verify_reset_code(customer_id, wrong, 5, token_hash, expires, now_utc())
                results.append(outcome["result"])
            except Exception as error:  # pragma: no cover - surfaced by the assertions
                results.append(f"error:{type(error).__name__}")

        threads = [threading.Thread(target=guess) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        accepted_wrong = [item for item in results if item in {"invalid", "invalid_locked"}]
        self.assertEqual(len(results), 12)
        self.assertEqual(len(accepted_wrong), 5, results)
        self.assertIsNone(customer_repository.get_reset(customer_id))

    def test_customer_correct_code_still_succeeds_after_four_wrong_guesses(self) -> None:
        customer_id = self._new_customer()
        self._issue_code(customer_id, "123456")
        wrong = auth_services._keyed_digest("otp", customer_id, "000000")
        right = auth_services._keyed_digest("otp", customer_id, "123456")
        token_hash = auth_services._keyed_digest("reset", customer_id, "token")
        expires = (now_utc() + timedelta(minutes=10)).isoformat(timespec="seconds")
        for _ in range(4):
            self.assertEqual(customer_repository.verify_reset_code(customer_id, wrong, 5, token_hash, expires, now_utc())["result"], "invalid")
        self.assertEqual(customer_repository.verify_reset_code(customer_id, right, 5, token_hash, expires, now_utc())["result"], "ok")
        record = customer_repository.get_reset(customer_id)
        self.assertEqual(record["reset_token_hash"], token_hash)
        self.assertEqual(record["attempts"], 0)

    def test_admin_concurrent_guesses_cannot_bypass_attempt_limit(self) -> None:
        digest = admin_auth_services._keyed_digest("otp", "654321")
        admin_repository.replace_reset_code(digest, (now_utc() + timedelta(minutes=5)).isoformat(timespec="seconds"), now_utc().isoformat(timespec="seconds"))
        wrong = admin_auth_services._keyed_digest("otp", "000000")
        token_hash = admin_auth_services._keyed_digest("reset", "unused")
        expires = (now_utc() + timedelta(minutes=10)).isoformat(timespec="seconds")
        results: list[str] = []
        barrier = threading.Barrier(10)

        def guess() -> None:
            barrier.wait()
            try:
                results.append(admin_repository.verify_reset_code(wrong, 5, token_hash, expires, now_utc())["result"])
            except Exception as error:  # pragma: no cover
                results.append(f"error:{type(error).__name__}")

        threads = [threading.Thread(target=guess) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len([item for item in results if item in {"invalid", "invalid_locked"}]), 5, results)
        self.assertIsNone(admin_repository.get_reset())


class GatewayEndpointTests(unittest.TestCase):
    def test_public_https_endpoint_is_accepted(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            self.assertEqual(gateway.endpoint_problem("https://provider.example.test/verify"), "")

    def test_private_and_metadata_endpoints_are_refused(self) -> None:
        for endpoint in (
            "http://169.254.169.254/latest/meta-data",
            "https://127.0.0.1/verify",
            "https://10.0.0.5/verify",
            "https://192.168.1.4/verify",
            "https://2130706433/verify",
            "https://[::1]/verify",
            "ftp://provider.example.test/verify",
            "https://user:pass@provider.example.test/verify",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(gateway.endpoint_problem(endpoint), endpoint)

    def test_remote_http_is_refused_but_local_development_is_allowed(self) -> None:
        self.assertIn("HTTPS", gateway.endpoint_problem("http://gateway.example.test/verify"))
        self.assertEqual(gateway.endpoint_problem("http://127.0.0.1:9000/verify"), "")

    def test_dns_answer_pointing_inward_is_refused(self) -> None:
        with mock.patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.1.2.3", 443))]):
            self.assertIn("private", gateway.endpoint_problem("https://provider.example.test/verify"))

    def test_optional_allow_list_is_enforced(self) -> None:
        with mock.patch.object(gateway, "settings", replace(settings, gateway_allowed_hosts=("approved.example.test",))):
            self.assertIn("allow-list", gateway.endpoint_problem("https://other.example.test/verify"))
            with mock.patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
                self.assertEqual(gateway.endpoint_problem("https://api.approved.example.test/verify"), "")

    def test_confirmation_never_contacts_a_rejected_endpoint(self) -> None:
        config = {"endpoint": "http://169.254.169.254/latest/meta-data", "api_key": "secret", "provider": "Test"}
        with mock.patch("httpx.post", side_effect=AssertionError("must not be called")):
            result = gateway.confirm_with_provider("GCash", "api", config, "REF-1234", 500.0)
        self.assertFalse(result["attempted"])
        self.assertFalse(result["confirmed"])
        self.assertIn("rejected", result["detail"])


class ReferenceClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        from backend.app.repositories import reference_claim_repository as claims

        self.claims = claims
        self.claims.initialize()
        with connection_scope() as connection:
            connection.execute("DELETE FROM reference_claims")

    def test_unique_key_is_enforced_by_the_database(self) -> None:
        with connection_scope() as connection:
            self.claims.apply(connection, [{"kind": "payment", "key": "ref1234", "context": "sub-1"}], "ORDER-1", "2026-01-01")
            with self.assertRaises(self.claims.DuplicateReference):
                self.claims.apply(connection, [{"kind": "payment", "key": "ref1234", "context": "sub-2"}], "ORDER-2", "2026-01-01")
        self.assertTrue(self.claims.is_used("payment", "ref1234"))

    def test_release_frees_a_reference_for_reuse(self) -> None:
        scope = __import__("backend.app.database.connection", fromlist=["connection_scope"]).connection_scope
        with scope() as connection:
            self.claims.apply(connection, [{"kind": "payment", "key": "ref5678"}], "ORDER-1", "2026-01-01")
            self.claims.apply(connection, [{"op": "release", "kind": "payment", "key": "ref5678"}], "ORDER-1", "2026-01-02")
            self.claims.apply(connection, [{"kind": "payment", "key": "ref5678", "context": "sub-2"}], "ORDER-1", "2026-01-03")
        self.assertFalse(self.claims.is_used("payment", "ref-not-used"))

    def test_tracking_reuse_is_allowed_only_in_the_current_cycle(self) -> None:
        scope = __import__("backend.app.database.connection", fromlist=["connection_scope"]).connection_scope
        with scope() as connection:
            self.claims.apply(connection, [{
                "kind": "tracking", "key": "jt123456", "context": "cycle:1", "allow_context": "cycle:1",
            }], "ORDER-1", "2026-01-01")
            # Same parcel, same cycle: allowed.
            self.claims.apply(connection, [{
                "kind": "tracking", "key": "jt123456", "context": "cycle:1", "allow_context": "cycle:1",
            }], "ORDER-1", "2026-01-02")
            # After a reprint the old parcel's number is archived and cannot be reused.
            self.claims.apply(connection, [{
                "op": "archive", "kind": "tracking", "key": "jt123456", "context": "archived:1",
            }], "ORDER-1", "2026-01-03")
            with self.assertRaises(self.claims.DuplicateReference):
                self.claims.apply(connection, [{
                    "kind": "tracking", "key": "jt123456", "context": "cycle:2", "allow_context": "cycle:2",
                }], "ORDER-1", "2026-01-04")


class EmailVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        customer_repository.initialize()
        auth_throttle_repository.initialize()
        auth_throttle_repository.clear()
        limiter.clear()
        self.sent: list[tuple[str, str, str, str | None]] = []

        def capture(recipient: str, subject: str, body: str, html_body: str | None = None) -> None:
            self.sent.append((recipient, subject, body, html_body))

        patchers = [
            mock.patch.object(email_service, "send_message", side_effect=capture),
        ]
        verified = replace(
            settings,
            require_email_verification=True,
            smtp_host="smtp.example.test",
            smtp_from="shop@example.test",
        )
        patchers.extend([
            mock.patch.object(auth_services, "settings", verified),
            mock.patch.object(auth_routes, "settings", verified),
        ])
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._context = TestClient(app)
        self.client = self._context.__enter__()
        self.addCleanup(lambda: self._context.__exit__(None, None, None))

    def _register(self) -> str:
        email = f"verify-{uuid.uuid4().hex[:10]}@example.test"
        response = self.client.post("/api/customer/register", json={
            "name": "Verify Customer", "email": email, "phone": "09175551234",
            "messenger": "", "password": "Audit-Pass-123!",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["email_verification"]["required"])
        return email

    def _code(self) -> str:
        self.assertTrue(self.sent)
        match = re.search(r"email verification code is: (\d{6})", self.sent[-1][2])
        self.assertIsNotNone(match)
        return match.group(1)  # type: ignore[union-attr]

    def test_registration_needs_a_code_before_it_can_sign_in(self) -> None:
        email = self._register()
        self.assertEqual(self.client.get("/api/customer/session").status_code, 401)
        blocked = self.client.post("/api/customer/session", json={"email": email, "password": "Audit-Pass-123!"})
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("Verify your email", blocked.json()["detail"])

        wrong = "000000" if self._code() != "000000" else "111111"
        self.assertEqual(self.client.post("/api/customer/email-verification/verify", json={
            "email": email, "code": wrong,
        }).status_code, 422)

        verified = self.client.post("/api/customer/email-verification/verify", json={
            "email": email, "code": self._code(),
        })
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()["customer"]["email_verified"])
        signed_in = self.client.post("/api/customer/session", json={"email": email, "password": "Audit-Pass-123!"})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)

    def test_verify_email_page_is_served(self) -> None:
        response = self.client.get("/verify-email")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-auth-page="verify-email"', response.text)
        self.assertIn('id="verify-email-form"', response.text)

    def test_unknown_address_gets_the_generic_request_response(self) -> None:
        before = len(self.sent)
        response = self.client.post("/api/customer/email-verification/request", json={
            "email": f"unknown-{uuid.uuid4().hex[:10]}@example.test",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("If an account exists", response.json()["message"])
        self.assertEqual(len(self.sent), before)


class CanonicalProvinceTests(unittest.TestCase):
    def test_province_aliases_resolve_to_the_psgc_name(self) -> None:
        for alias in ("Cavite", "Province of Cavite", "Cavite Province", "cavite (region IV-A)"):
            with self.subTest(alias=alias):
                self.assertEqual(location_services.canonical_province(alias), "Cavite")
        self.assertIsNone(location_services.canonical_province("Not a Province"))

    def test_region_fallback_accepts_a_legacy_province_alias(self) -> None:
        self.assertEqual(shop_settings_services.region_of("Province of Cebu"), "visayas")
        self.assertEqual(shop_settings_services.region_of("Davao del Sur Province"), "mindanao")

    def test_configured_area_is_matched_by_province_key(self) -> None:
        settings_copy = {
            "delivery_enabled": True,
            "areas": [{
                "id": "cavite-area", "label": "Cavite", "region": "luzon",
                "provinces": ["Province of Cavite"], "fee": 75.0,
                "estimate_min_days": 2, "estimate_max_days": 4, "active": True,
            }],
        }
        with mock.patch.object(shop_settings_services, "fulfillment_settings", return_value=settings_copy):
            quote = shop_settings_services.delivery_quote("Cavite Province")
            self.assertEqual((quote["area_id"], quote["delivery_fee"]), ("cavite-area", 75.0))
            with self.assertRaises(Exception):
                shop_settings_services.delivery_quote("Laguna", "cavite-area")

    def test_admin_area_provinces_are_stored_in_canonical_form(self) -> None:
        cleaned = shop_settings_services._clean_area({
            "id": "cavite", "label": "Cavite", "region": "luzon",
            "provinces": ["Province of Cavite", "cavite", "Unknown Province"],
            "fee": 0, "active": True,
        }, 1)
        self.assertEqual(cleaned["provinces"], ["Cavite", "Unknown Province"])


class _Batch4Environment(unittest.TestCase):
    """An isolated database and upload directory for backup/storage tests."""

    signing_key = "backup-key-for-regression-tests-0123456789"

    def setUp(self) -> None:
        from backend.app.repositories import (
            catalog_repository,
            chatbot_repository,
            design_repository,
            notification_repository,
            order_repository,
            package_repository,
            payment_method_repository,
            reference_claim_repository,
            session_revocation_repository,
            size_repository,
        )

        temporary = tempfile.TemporaryDirectory(prefix="impress-batch4-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database_path = self.directory / "test.sqlite3"
        self.upload_dir = self.directory / "uploads"
        self.upload_dir.mkdir()

        backup_settings = replace(
            settings,
            upload_dir=self.upload_dir,
            file_storage="disk",
            secret_key="x" * 48,
            backup_signing_key=self.signing_key,
            previous_backup_signing_keys=(),
        )
        file_settings = replace(settings, upload_dir=self.upload_dir, file_storage="disk")
        for patcher in (
            mock.patch("backend.app.database.connection.DATABASE_PATH", self.database_path),
            mock.patch.object(backup_services, "settings", backup_settings),
            mock.patch.object(file_services, "settings", file_settings),
            mock.patch.object(file_services, "UPLOAD_DIR", self.upload_dir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        for initialize in (
            customer_repository.initialize,
            admin_repository.initialize,
            auth_throttle_repository.initialize,
            session_revocation_repository.initialize,
            order_repository.initialize,
            payment_method_repository.initialize,
            catalog_repository.initialize,
            size_repository.initialize,
            file_repository.initialize,
            package_repository.initialize,
            design_repository.initialize,
            chatbot_repository.initialize,
            reference_claim_repository.initialize,
        ):
            initialize()

    def _write_disk_file(self, key: str, contents: bytes) -> Path:
        path = self.upload_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path


class BackupFileTests(_Batch4Environment):
    def test_v2_backup_restores_database_files_and_disk_files(self) -> None:
        stored_name = f"{secrets.token_hex(16)}.png"
        file_repository.store_chunks(stored_name, "image/png", [b"database-file-bytes"])
        self._write_disk_file("keep.txt", b"disk-file-bytes")
        content, filename, event = backup_services.create_backup("admin")
        document = json.loads(content)
        self.assertEqual(document["version"], 2)
        self.assertTrue(filename.startswith("impression-printshop-backup-"))
        self.assertEqual(event["files"], 1)
        self.assertEqual(base64.b64decode(document["files"]["keep.txt"]), b"disk-file-bytes")
        self.assertIn(stored_name, {row[0] for row in document["tables"]["stored_files"]["rows"]})

        file_repository.delete_stored_file(stored_name)
        (self.upload_dir / "keep.txt").write_bytes(b"changed")
        self._write_disk_file("new-since-backup.txt", b"new")

        result = backup_services.restore_backup(document, "RESTORE", "admin")
        self.assertEqual(result["files_restored"], 1)
        self.assertIsNotNone(file_repository.stored_file(stored_name))
        self.assertEqual(b"".join(file_repository.iter_chunks(stored_name)), b"database-file-bytes")
        self.assertEqual((self.upload_dir / "keep.txt").read_bytes(), b"disk-file-bytes")
        self.assertFalse((self.upload_dir / "new-since-backup.txt").exists())

    def test_v1_backup_leaves_the_upload_directory_untouched(self) -> None:
        content, _, _ = backup_services.create_backup("admin")
        document = json.loads(content)
        document.pop("files", None)
        document.pop("file_count", None)
        document["version"] = 1
        document["checksum"] = backup_services._checksum(document["tables"], {}, 1)
        document["signature"] = backup_services._signature(document["checksum"], document["created_at"], 1)

        legacy = self._write_disk_file("legacy-only.txt", b"still-here")
        self._write_disk_file("catalog/legacy.png", b"\x89PNG\r\n\x1a\n")
        result = backup_services.restore_backup(document, "RESTORE", "admin")
        self.assertEqual(result["files_restored"], 0)
        self.assertEqual(legacy.read_bytes(), b"still-here")
        self.assertTrue((self.upload_dir / "catalog" / "legacy.png").exists())

    def test_previous_signing_key_can_restore_an_old_backup(self) -> None:
        content, _, _ = backup_services.create_backup("admin")
        document = json.loads(content)
        rotated = replace(
            settings,
            upload_dir=self.upload_dir,
            file_storage="disk",
            secret_key="x" * 48,
            backup_signing_key="rotated-backup-key-for-regression-tests-9876",
            previous_backup_signing_keys=(self.signing_key,),
        )
        with mock.patch.object(backup_services, "settings", rotated):
            tables, files, version = backup_services.validate_backup(document)
        self.assertEqual(version, 2)
        self.assertTrue(tables)
        self.assertIsInstance(files, dict)

        without_history = replace(rotated, previous_backup_signing_keys=())
        with mock.patch.object(backup_services, "settings", without_history):
            with self.assertRaises(HTTPException) as failure:
                backup_services.validate_backup(document)
        self.assertIn("signing key", failure.exception.detail)

    def test_restore_rejects_an_unsafe_disk_path(self) -> None:
        content, _, _ = backup_services.create_backup("admin")
        document = json.loads(content)
        document["files"]["../escape.txt"] = document["files"].get("keep.txt", base64.b64encode(b"x").decode())
        document["checksum"] = backup_services._checksum(document["tables"], document["files"], 2)
        document["signature"] = backup_services._signature(document["checksum"], document["created_at"], 2)
        with self.assertRaises(HTTPException) as failure:
            backup_services.restore_backup(document, "RESTORE", "admin")
        self.assertIn("unsafe", failure.exception.detail)


class ProductionStorageValidationTests(unittest.TestCase):
    def _settings(self, **changes: object) -> object:
        return replace(
            settings,
            environment="production",
            secret_key="s" * 48,
            backup_signing_key="b" * 48,
            admin_email="admin@example.test",
            admin_password_hash="pbkdf2_sha256$test",
            allowed_hosts=("shop.example.test",),
            require_email_verification=False,
            **changes,
        )

    def test_turso_production_rejects_disk_storage(self) -> None:
        environment = {
            "PRINTSHOP_SECRET_KEY": "s" * 48,
            "PRINTSHOP_DATABASE_PATH": str(Path(tempfile.gettempdir()) / "validate.sqlite3"),
            "PRINTSHOP_UPLOAD_DIR": str(Path(tempfile.gettempdir()) / "validate-uploads"),
            "PRINTSHOP_ALLOWED_HOSTS": "shop.example.test",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(RuntimeError) as failure:
                self._settings(turso_database_url="libsql://example.turso.io", file_storage="disk").validate()
        self.assertIn("temporary filesystem", str(failure.exception))

    def test_temporary_upload_directory_is_rejected_in_production(self) -> None:
        environment = {
            "PRINTSHOP_SECRET_KEY": "s" * 48,
            "PRINTSHOP_DATABASE_PATH": str(Path(tempfile.gettempdir()) / "validate.sqlite3"),
            "PRINTSHOP_UPLOAD_DIR": str(Path(tempfile.gettempdir()) / "validate-uploads"),
            "PRINTSHOP_ALLOWED_HOSTS": "shop.example.test",
        }
        temporary_uploads = Path(tempfile.gettempdir()) / "impress-production-uploads"
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(RuntimeError) as failure:
                self._settings(
                    upload_dir=temporary_uploads,
                    file_storage="disk",
                    behind_vercel_proxy=False,
                    turso_database_url="",
                ).validate()
        self.assertIn("temporary directory", str(failure.exception))

    def test_database_storage_is_accepted_in_production(self) -> None:
        environment = {
            "PRINTSHOP_SECRET_KEY": "s" * 48,
            "PRINTSHOP_DATABASE_PATH": str(Path(tempfile.gettempdir()) / "validate.sqlite3"),
            "PRINTSHOP_UPLOAD_DIR": str(Path(tempfile.gettempdir()) / "validate-uploads"),
            "PRINTSHOP_ALLOWED_HOSTS": "shop.example.test",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            self._settings(
                upload_dir=Path(tempfile.gettempdir()) / "impress-production-uploads",
                file_storage="database",
                behind_vercel_proxy=True,
            ).validate()


class OrphanCleanupTests(_Batch4Environment):
    def test_cleanup_removes_only_old_unreferenced_artifacts(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        old_session = "upl_old_session"
        with connection_scope() as connection:
            connection.execute(
                "INSERT INTO upload_sessions (id, customer_id, product_id, field_key, name, extension, mime_type, size, created_at)"
                " VALUES (?, ?, ?, '', 'old.png', '.png', 'image/png', 1, ?)",
                (old_session, "customer-1", "product-1", old),
            )
            connection.execute("INSERT INTO file_chunks (owner, chunk_index, data) VALUES (?, 0, ?)", (old_session, b"old"))
            connection.execute("INSERT INTO file_chunks (owner, chunk_index, data) VALUES ('orphan-owner', 0, ?)", (b"orphan",))
            connection.execute(
                "INSERT INTO stored_files (filename, mime_type, size, chunk_count, created_at) VALUES (?, 'image/png', 0, 0, ?)",
                (f"{secrets.token_hex(16)}.png", old),
            )

        unreferenced = f"{secrets.token_hex(16)}.png"
        referenced = f"{secrets.token_hex(16)}.png"
        fresh = f"{secrets.token_hex(16)}.png"
        for name in (unreferenced, referenced):
            self._write_disk_file(name, b"old-disk-file").touch()
            os.utime(self.upload_dir / name, (time.time() - 25 * 3600,) * 2)
        self._write_disk_file(fresh, b"fresh-disk-file")
        admin_repository.set_setting("backup.orphan-reference", referenced)

        result = file_services.cleanup_orphans()
        self.assertEqual(result["sessions"], 1)
        # The expired session's chunk is removed with its session; this counter is the leftover orphan.
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["stored_files"], 1)
        self.assertEqual(result["disk_files"], 1)
        self.assertFalse((self.upload_dir / unreferenced).exists())
        self.assertTrue((self.upload_dir / referenced).exists())
        self.assertTrue((self.upload_dir / fresh).exists())
        with connection_scope() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM upload_sessions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM file_chunks").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stored_files").fetchone()[0], 0)


class CompensatingFileTests(_Batch4Environment):
    def test_unexpected_exception_removes_files_saved_by_the_service(self) -> None:
        @file_services.compensating_files
        def fail_after_saving() -> None:
            metadata = file_services.new_metadata("proof.png", ".png", "image/png", len(PNG_SIGNATURE))
            file_services.save(metadata, PNG_SIGNATURE)
            raise RuntimeError("unexpected database failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected database failure"):
            fail_after_saving()
        self.assertEqual(list(self.upload_dir.glob("*.png")), [])

    def test_nested_saved_files_scopes_clean_up_together(self) -> None:
        outer = file_services.new_metadata("outer.png", ".png", "image/png", len(PNG_SIGNATURE))
        inner = file_services.new_metadata("inner.png", ".png", "image/png", len(PNG_SIGNATURE))

        @file_services.compensating_files
        def fail_in_inner_scope() -> None:
            file_services.save(outer, PNG_SIGNATURE)
            with file_services.saved_files():
                file_services.save(inner, PNG_SIGNATURE)
                raise ValueError("failed after the inner save")

        with self.assertRaises(ValueError):
            fail_in_inner_scope()
        self.assertFalse((self.upload_dir / outer["filename"]).exists())
        self.assertFalse((self.upload_dir / inner["filename"]).exists())


class ExclusiveWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="impress-exclusive-")
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch("backend.app.database.connection.DATABASE_PATH", Path(temporary.name) / "test.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nested_calls_reuse_the_same_transaction(self) -> None:
        with exclusive_write() as outer:
            with exclusive_write() as inner:
                self.assertIs(outer, inner)

    def test_concurrent_writers_are_serialized(self) -> None:
        failures: list[str] = []
        barrier = threading.Barrier(2)

        def worker(number: int) -> None:
            try:
                barrier.wait()
                with exclusive_write() as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS exclusive_test (id INTEGER PRIMARY KEY, worker INTEGER NOT NULL)")
                    time.sleep(0.05)
                    connection.execute("INSERT INTO exclusive_test (worker) VALUES (?)", (number,))
            except Exception as error:  # pragma: no cover - surfaced by assertions
                failures.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=worker, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(failures, [])
        with connection_scope() as connection:
            rows = connection.execute("SELECT worker FROM exclusive_test ORDER BY worker").fetchall()
        self.assertEqual([row["worker"] for row in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
