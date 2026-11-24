"""Regression tests for persistent workflow URLs and change detection.

The backend checks exercise real sessions, saved orders, and the same write actions the workspace uses.
The frontend is covered with static source checks because this repository has no JavaScript unit runner:
they guard the routing/sync contracts that make a refresh or Back/Forward restore the workflow.
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


_test_directory = tempfile.TemporaryDirectory(prefix="impress-navigation-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.main import app  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Correct-Horse-Battery-1"
ADMIN_EMAIL = "admin@example.test"
MUG_OPTIONS = {"size": "11-oz", "print_area": "one-side", "inner_color": "white-classic", "packaging": "no-packaging"}
STATE_KEYS = {
    "reference", "version", "status", "payment_status", "work_status", "fulfillment_status",
    "work_approval_status", "pending_submissions", "confirmed_payments", "updated_at",
}


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def _next_parameter(response) -> str:
    location = response.headers["location"]
    values = parse_qs(urlsplit(location).query).get("next", [])
    return values[0] if values else ""


class WorkflowNavigationBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()
        cls.admin = TestClient(app)
        cls.admin.cookies.set(SESSION_COOKIE, create_session_token(ADMIN_EMAIL, "admin"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()
        self.shopper, self.email = self._new_customer()
        self.reference = self._place_order()

    # ------------------------------------------------------------ helpers

    def _new_customer(self) -> tuple[TestClient, str]:
        shopper = TestClient(app)
        response = shopper.post("/api/customer/register", json={
            "name": "Navigation Customer",
            "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567",
            "messenger": "",
            "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return shopper, response.json()["customer"]["email"]

    def _place_order(self) -> str:
        response = self.shopper.post("/api/orders", json={
            "customer": {"name": "Navigation Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": "white-mug"},
            "customization": {"quantity": 2, "options": MUG_OPTIONS, "notes": ""},
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": [],
        }, headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["reference"]

    def _state(self, client: TestClient, role: str = "account") -> dict:
        base = "/api/admin/orders" if role == "admin" else "/api/account/orders"
        response = client.get(f"{base}/{self.reference}/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _admin_post(self, action: str, body: dict) -> dict:
        payload = {**body, "expected_version": self._state(self.admin, "admin")["version"]}
        response = self.admin.post(f"/api/admin/orders/{self.reference}/{action}", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ------------------------------------------------------------ page routes

    def test_customer_order_links_require_a_customer_session_and_keep_the_destination(self) -> None:
        for suffix in ("", "/payment", "/approval"):
            with self.subTest(suffix=suffix):
                target = f"/customer/orders/{self.reference}{suffix}"
                anonymous = TestClient(app)
                redirected = anonymous.get(target, follow_redirects=False)
                self.assertEqual(redirected.status_code, 303)
                self.assertEqual(urlsplit(redirected.headers["location"]).path, "/sign-in")
                self.assertEqual(_next_parameter(redirected), target)

                page = self.shopper.get(target)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("/assets/workflow-sync.js", page.text)
                self.assertIn("/assets/portal.js", page.text)

    def test_public_customer_portal_still_opens_without_a_session(self) -> None:
        response = TestClient(app).get("/customer-portal")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/assets/portal.js", response.text)

    def test_admin_order_links_require_an_admin_session_and_keep_the_destination(self) -> None:
        for suffix in ("", "/payments"):
            with self.subTest(suffix=suffix):
                target = f"/admin/orders/{self.reference}{suffix}"
                anonymous = TestClient(app)
                redirected = anonymous.get(target, follow_redirects=False)
                self.assertEqual(redirected.status_code, 303)
                self.assertEqual(urlsplit(redirected.headers["location"]).path, "/admin/login")
                self.assertEqual(_next_parameter(redirected), target)

                page = self.admin.get(target)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("/assets/workflow-sync.js", page.text)
                self.assertIn('data-admin-view="orders"', page.text)

    def test_existing_admin_page_route_keeps_its_query_in_next(self) -> None:
        target = "/admin/payments?verification=pending"
        response = TestClient(app).get(target, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(_next_parameter(response), target)

    # ------------------------------------------------------------ state endpoints

    def test_customer_state_is_small_read_only_and_ownership_checked(self) -> None:
        state = self._state(self.shopper)
        self.assertEqual(set(state), STATE_KEYS)
        self.assertEqual(state["reference"], self.reference)
        self.assertIsInstance(state["version"], int)
        self.assertGreater(state["version"], 0)

        anonymous = TestClient(app)
        self.assertEqual(anonymous.get(f"/api/account/orders/{self.reference}/state").status_code, 401)
        other_customer, _ = self._new_customer()
        self.assertEqual(other_customer.get(f"/api/account/orders/{self.reference}/state").status_code, 404)

    def test_admin_state_rejects_customer_sessions_and_missing_orders(self) -> None:
        state = self._state(self.admin, "admin")
        self.assertEqual(set(state), STATE_KEYS)
        self.assertEqual(state["reference"], self.reference)
        self.assertEqual(self.shopper.get(f"/api/admin/orders/{self.reference}/state").status_code, 401)
        self.assertEqual(self.admin.get("/api/admin/orders/NO-SUCH-ORDER/state").status_code, 404)

    def test_state_version_follows_real_workflow_actions(self) -> None:
        initial = self._state(self.shopper)
        reviewed = self._admin_post("status", {"status": "Under Review"})
        after_review = self._state(self.shopper)
        self.assertEqual(after_review["version"], initial["version"] + 1)
        self.assertEqual(after_review["status"], "Under Review")
        self.assertEqual(reviewed["version"], after_review["version"])

        quoted = self._admin_post("quotation", {"total_amount": 1000, "payment_instructions": "GCash 0917 000 0000"})
        after_quote = self._state(self.shopper)
        self.assertEqual(after_quote["version"], after_review["version"] + 1)
        self.assertEqual(quoted["version"], after_quote["version"])

        accepted = self.shopper.post(f"/api/orders/{self.reference}/quotation/accept", json={
            "email": self.email,
            "quotation_revision": quoted["quotation"]["revision"],
        })
        self.assertEqual(accepted.status_code, 200, accepted.text)
        after_accept = self._state(self.admin, "admin")
        self.assertEqual(after_accept["version"], after_quote["version"] + 1)
        self.assertEqual(after_accept["status"], "Awaiting Down Payment")
        self.assertEqual(after_accept["version"], accepted.json()["version"])

        proof = b"\x89PNG\r\n\x1a\n" + uuid.uuid4().hex.encode() * 2
        submitted = self.shopper.post(f"/api/orders/{self.reference}/payments", json={
            "email": self.email,
            "reference": f"PAY-{uuid.uuid4().hex[:10].upper()}",
            "proof": {"name": "gcash.png", "dataUrl": "data:image/png;base64," + base64.b64encode(proof).decode()},
        })
        self.assertEqual(submitted.status_code, 200, submitted.text)
        after_payment = self._state(self.shopper)
        self.assertEqual(after_payment["version"], after_accept["version"] + 1)
        self.assertEqual(after_payment["pending_submissions"], 1)
        # A pending receipt is deliberately not counted as paid until an administrator verifies it.
        self.assertEqual(after_payment["confirmed_payments"], 0)


class WorkflowNavigationStaticTests(unittest.TestCase):
    def test_backend_registers_stable_page_routes_and_state_endpoints(self) -> None:
        main = _read("backend/app/main.py")
        self.assertIn('f"/customer/orders/{{reference}}{_suffix}"', main)
        self.assertIn("_customer_order_page(\"customer-portal.html\")", main)
        self.assertIn('f"/admin/orders/{{reference}}{_suffix}"', main)
        self.assertIn("_admin_page(\"OrderManagement.html\")", main)
        self.assertIn("/sign-in?{urlencode({'next': target})}", main)
        self.assertIn("/admin/login?{urlencode({'next': target})}", main)

        routes = _read("backend/app/routes/order.py")
        self.assertIn('@router.get("/api/account/orders/{reference}/state")', routes)
        self.assertIn('@router.get("/api/admin/orders/{reference}/state")', routes)

        services = _read("backend/app/services/order_services.py")
        self.assertIn("def workflow_state(", services)
        self.assertIn("def account_order_state(", services)
        self.assertIn("def admin_order_state(", services)

    def test_sync_module_uses_one_poller_and_cross_tab_signals(self) -> None:
        sync = _read("frontend/assets/workflow-sync.js")
        self.assertIn("const watchers = new Map();", sync)
        self.assertIn("if (!timer) timer = window.setInterval(tick, POLL_MS);", sync)
        self.assertIn('document.visibilityState !== "visible"', sync)
        self.assertIn('window.addEventListener("focus", onVisible)', sync)
        self.assertIn('window.addEventListener("online", onVisible)', sync)
        self.assertIn("new BroadcastChannel(CHANNEL_NAME)", sync)
        self.assertIn("localStorage.setItem(STORAGE_PREFIX", sync)
        self.assertIn("signal: controller.signal", sync)
        self.assertIn("window.PrintShopWorkflowSync = { watch, tick, stop, POLL_MS };", sync)

    def test_customer_portal_restores_from_the_url_and_preserves_forms(self) -> None:
        portal = _read("frontend/assets/portal.js")
        self.assertIn(r"const ORDER_ROUTE = /^\/customer\/orders\/([^/]+)(?:\/(payment|approval))?\/?$/;", portal)
        self.assertIn("const sessionReady = window.PrintShopAccount?.ready", portal)
        self.assertIn("void sessionReady.then((customer) =>", portal)
        self.assertIn('window.addEventListener("popstate"', portal)
        self.assertIn("void loadOrder(route.reference, { fromRoute: true, step: currentStep });", portal)
        self.assertIn("function formBusy()", portal)
        self.assertIn("return field.checked;", portal)
        self.assertIn("function applyLiveStatus(order)", portal)
        self.assertIn("applyLiveStatus(order);", portal)
        self.assertIn("void quietRefresh(reference);", portal)
        self.assertIn('window.location.replace(`/sign-in?${new URLSearchParams({ next: target })}`)', portal)
        self.assertNotIn('fetch("/api/customer/session"', portal)

        account = _read("frontend/assets/account.js")
        self.assertIn("window.PrintShopAccount = { ready: sessionReady };", account)

    def test_admin_workspace_keeps_deep_links_filters_and_dirty_forms(self) -> None:
        dashboard = _read("frontend/assets/admin/dashboard.js")
        self.assertIn("const ADMIN_ORDER_PATH =", dashboard)
        self.assertIn('if (ADMIN_ORDER_PATH.test(window.location.pathname)) return "orders";', dashboard)
        self.assertIn("function syncOrderUrl(reference, focus = \"\")", dashboard)
        self.assertIn("syncOrderUrl(reference, focus);", dashboard)
        self.assertIn('void navigate("orders", false).then(() => openOrder(route.reference, { focus: route.focus }));', dashboard)
        self.assertIn('if (adminOrderRoute()) window.history.pushState', dashboard)
        self.assertIn("container.dataset.dirty = \"true\";", dashboard)
        self.assertIn('role: "admin",', dashboard)
        self.assertIn("function syncVerificationParam(value)", dashboard)
        self.assertIn("function refreshOpenOrder(", dashboard)

    def test_sign_in_redirects_only_accept_safe_next_paths(self) -> None:
        login = _read("frontend/assets/admin/login.js")
        self.assertIn('url.pathname.startsWith("/admin/")', login)
        self.assertIn("window.location.assign(nextPath());", login)
        core = _read("frontend/assets/admin/admin-core.js")
        self.assertIn("new URLSearchParams({ next: target })", core)

    def test_every_workspace_shell_loads_the_sync_module(self) -> None:
        self.assertIn('/assets/workflow-sync.js', _read("frontend/customer/customer-portal.html"))
        pages = (
            "CancellationsRefunds", "Chatbot", "CustomerManagement", "Dashboard", "FulfillmentManagement",
            "NotificationManagement", "OrderManagement", "PaymentMonitoring", "ProductionMonitoring",
            "SalesReports", "ServicesProducts", "Settings",
        )
        for page in pages:
            with self.subTest(page=page):
                self.assertIn('/assets/workflow-sync.js', _read(f"frontend/admin/{page}.html"))


if __name__ == "__main__":
    unittest.main()
