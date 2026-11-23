"""Regression tests for the audit remediation (P0 security fixes).

All data is synthetic and stored in a temporary database.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock


_test_directory = tempfile.TemporaryDirectory(prefix="impress-security-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")
os.environ.setdefault("PRINTSHOP_ADMIN_EMAIL", "admin@example.test")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from starlette.requests import Request  # noqa: E402

from backend.app.core import security  # noqa: E402
from backend.app.core.config import settings  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import auth_throttle_repository as throttle_store  # noqa: E402
from backend.app.repositories import session_revocation_repository  # noqa: E402
from backend.app.routes import auth as auth_routes  # noqa: E402
from backend.app.services import export_services  # noqa: E402


PROJECT_DIR = Path(__file__).resolve().parents[2]
ADMIN_DIR = PROJECT_DIR / "frontend" / "admin"


def request_with(headers: dict[str, str], client: tuple[str, int] = ("127.0.0.1", 1234)) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": client,
        "server": ("testserver", 80),
    })


class SessionRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        session_revocation_repository.initialize()
        session_revocation_repository.clear()
        throttle_store.initialize()
        throttle_store.clear()
        limiter.clear()

    def test_customer_logout_revokes_a_copied_token(self) -> None:
        with TestClient(app) as client:
            email = f"revoke-{uuid.uuid4().hex[:8]}@example.test"
            body = {"email": email, "password": "Audit-Pass-123!", "name": "Revoke Me", "phone": "09175551234"}
            self.assertEqual(client.post("/api/customer/register", json=body).status_code, 201)
            login = client.post("/api/customer/session", json={"email": email, "password": "Audit-Pass-123!", "remember": True})
            self.assertEqual(login.status_code, 200)
            copied = client.cookies.get(SESSION_COOKIE)
            self.assertIsNotNone(copied)
            self.assertEqual(client.delete("/api/customer/session").status_code, 204)

            client.cookies.set(SESSION_COOKIE, copied)
            self.assertEqual(client.get("/api/customer/session").status_code, 401)

    def test_admin_logout_revokes_a_copied_token(self) -> None:
        with TestClient(app) as client:
            token = create_session_token(settings.admin_email, "admin")
            client.cookies.set(SESSION_COOKIE, token)
            self.assertEqual(client.get("/api/admin/session").status_code, 200)
            self.assertEqual(client.delete("/api/admin/session").status_code, 204)
            client.cookies.set(SESSION_COOKIE, token)
            self.assertEqual(client.get("/api/admin/session").status_code, 401)

    def test_other_tokens_for_the_same_account_remain_valid(self) -> None:
        with TestClient(app) as client:
            email = f"other-{uuid.uuid4().hex[:8]}@example.test"
            body = {"email": email, "password": "Audit-Pass-123!", "name": "Other Device", "phone": "09175551234"}
            self.assertEqual(client.post("/api/customer/register", json=body).status_code, 201)
            first = client.cookies.get(SESSION_COOKIE)
            second = create_session_token(client.get("/api/customer/session").json()["customer"]["id"], "customer")
            self.assertEqual(client.delete("/api/customer/session").status_code, 204)
            client.cookies.set(SESSION_COOKIE, second)
            self.assertEqual(client.get("/api/customer/session").status_code, 200)
            client.cookies.set(SESSION_COOKIE, first)
            self.assertEqual(client.get("/api/customer/session").status_code, 401)


class ExcelInjectionTests(unittest.TestCase):
    def test_every_string_cell_is_stored_as_text(self) -> None:
        report = export_services.Report(
            title="=1+1",
            slug="test",
            period="+SUM(1,2)",
            filters=[("Customer", "@HYPERLINK(\"https://example.test\")"), ("Safe", "ordinary text")],
            summary=[("Revenue", "=2+2", "text")],
            sections=[export_services.Section(
                title="Customers",
                columns=[export_services.Column("name", "=3+3"), export_services.Column("note", "Notes")],
                rows=[{"name": "=4+4", "note": "-5+5"}],
                note="=6+6",
            )],
        )
        workbook = load_workbook(io.BytesIO(export_services.render_xlsx(report)))
        formulas: list[str] = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        formulas.append(f"{sheet.title}!{cell.coordinate}")
        self.assertEqual(formulas, [])
        self.assertEqual(workbook["Summary"]["B7"].value, "'@HYPERLINK(\"https://example.test\")")
        self.assertEqual(workbook["Customers"]["A5"].value, "'=4+4")


class ProxyTrustTests(unittest.TestCase):
    def test_client_supplied_forwarded_header_is_ignored(self) -> None:
        with mock.patch.object(auth_routes, "settings", replace(settings, behind_vercel_proxy=False, trusted_proxy_ips=())):
            request = request_with({"X-Forwarded-For": "198.51.100.7", "Origin": "https://example.test"})
            self.assertIsNone(auth_routes._client_address(request))

    def test_explicitly_trusted_proxy_is_used(self) -> None:
        with mock.patch.object(auth_routes, "settings", replace(settings, behind_vercel_proxy=False, trusted_proxy_ips=("127.0.0.1",))):
            request = request_with({"X-Forwarded-For": "203.0.113.4, 198.51.100.9"})
            self.assertEqual(auth_routes._client_address(request), "198.51.100.9")

    def test_vercel_real_ip_remains_trusted(self) -> None:
        with mock.patch.object(auth_routes, "settings", replace(settings, behind_vercel_proxy=True, trusted_proxy_ips=())):
            request = request_with({"X-Real-IP": "203.0.113.8"})
            self.assertEqual(auth_routes._client_address(request), "203.0.113.8")

    def test_production_uses_database_throttles(self) -> None:
        with mock.patch.object(type(settings), "production", True):
            self.assertIs(auth_routes._auth_throttle_store(), throttle_store)
        with mock.patch.object(type(settings), "production", False):
            self.assertIs(auth_routes._auth_throttle_store(), limiter)

    def test_public_scheme_is_used_for_same_origin(self) -> None:
        with mock.patch.object(security, "settings", replace(settings, public_scheme="https", cors_origins=())):
            self.assertIsNone(security.require_same_origin(request_with({"Origin": "https://testserver"})))
            with self.assertRaises(HTTPException) as raised:
                security.require_same_origin(request_with({"Origin": "http://testserver"}))
            self.assertEqual(raised.exception.status_code, 403)

    def test_render_deployment_does_not_trust_arbitrary_proxies(self) -> None:
        text = (PROJECT_DIR / "render.yaml").read_text(encoding="utf-8")
        self.assertNotIn('value: "*"', text)
        self.assertIn("PRINTSHOP_PUBLIC_SCHEME", text)
        self.assertNotIn("--proxy-headers", text)


class AdminNavigationScriptTests(unittest.TestCase):
    def test_every_admin_shell_loads_size_admin(self) -> None:
        shells = [path for path in ADMIN_DIR.glob("*.html") if "dashboard.js" in path.read_text(encoding="utf-8")]
        self.assertGreaterEqual(len(shells), 10)
        for path in shells:
            with self.subTest(path=path.name):
                self.assertIn("/assets/admin/size-admin.js", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
