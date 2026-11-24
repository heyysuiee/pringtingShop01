import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from backend.app.core.config import settings
from backend.app.core.security import session_claims
from backend.app.database.connection import exclusive_write
from backend.app.repositories import (
    admin_repository,
    auth_throttle_repository,
    catalog_repository,
    chatbot_repository,
    customer_repository,
    design_repository,
    file_repository,
    order_repository,
    package_repository,
    payment_method_repository,
    session_revocation_repository,
    size_repository,
)
from backend.app.routes.admin import router as admin_router
from backend.app.routes.auth import router as auth_router
from backend.app.routes.catalog import router as catalog_router
from backend.app.routes.chatbot import router as chatbot_router
from backend.app.routes.operations import router as operations_router
from backend.app.routes.order import router as order_router
from backend.app.routes.uploads import router as uploads_router
from backend.app.services import file_services


logger = logging.getLogger("printshop")
PROJECT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_DIR / "frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"
CUSTOMER_DIR = FRONTEND_DIR / "customer"
ADMIN_DIR = FRONTEND_DIR / "admin"

# Public storefront pages: URL path -> file in frontend/customer.
CUSTOMER_PAGES = {
    "/": "index.html",
    "/home": "index.html",
    "/services": "services.html",
    "/order": "order.html",
    "/sample-work": "sample-work.html",
    "/customer-portal": "customer-portal.html",
    "/sign-in": "sign-in.html",
    "/create-account": "create-account.html",
    "/verify-email": "verify-email.html",
    "/forgot-password": "forgot-password.html",
}
# Admin pages require an administrator session: URL path -> file in frontend/admin.
ADMIN_PAGES = {
    "/admin/dashboard": "Dashboard.html",
    "/admin/orders": "OrderManagement.html",
    "/admin/production": "ProductionMonitoring.html",
    "/admin/fulfillment": "FulfillmentManagement.html",
    "/admin/refunds": "CancellationsRefunds.html",
    "/admin/customers": "CustomerManagement.html",
    "/admin/services": "ServicesProducts.html",
    "/admin/payments": "PaymentMonitoring.html",
    "/admin/reports": "SalesReports.html",
    "/admin/notifications": "NotificationManagement.html",
    "/admin/settings": "Settings.html",
    "/admin/chatbot": "Chatbot.html",
}
# Old or alternative URLs kept working.
REDIRECTS = {
    "/account": "/customer-portal",
    "/finished-works": "/sample-work",
    "/signin": "/sign-in",
    "/register": "/create-account",
    "/sign-up": "/create-account",
    # Package Management and Pricing Management were merged into Services & Products.
    "/admin/packages": "/admin/services#packages",
    "/admin/designs": "/admin/services#designs",
    "/admin/pricing": "/admin/services",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    if not settings.secret_key_persistent:
        logger.warning(
            "PRINTSHOP_SECRET_KEY is not set, so a temporary key is used: every restart signs all customers out "
            "and cancels password resets in progress. Set it in the project-root .env file."
        )
    # One write transaction covers every schema change and seed marker. Two workers starting together
    # cannot both seed the catalog, sizes, or FAQs (QA-023); the loser waits and sees the committed data.
    with exclusive_write():
        customer_repository.initialize()
        admin_repository.initialize()
        auth_throttle_repository.initialize()
        session_revocation_repository.initialize()
        order_repository.initialize()
        # E-wallet payment methods (GCash active, Maya inactive until the shop enters its account).
        payment_method_repository.initialize()
        catalog_repository.initialize()
        # The standard size price lists (Tarpaulin sizes) reference catalog categories.
        size_repository.initialize()
        # Chunked uploads and order files kept in the database (PRINTSHOP_FILE_STORAGE=database).
        file_repository.initialize()
        # Packages reference catalog products, so this runs after the catalog is seeded/migrated.
        package_repository.initialize()
        # Shop-made designs (seeded with TEMPORARY SAMPLE designs until the shop uploads its own).
        design_repository.initialize()
        # Chatbot conversations, inquiries, and FAQs (links to customer accounts, so after customers).
        chatbot_repository.initialize()
    # Remove uploads abandoned by crashed or interrupted requests. A 24-hour grace period protects
    # files another request may still be writing.
    cleanup = file_services.cleanup_orphans()
    if any(cleanup.values()):
        logger.info("Removed orphaned upload data on startup: %s", cleanup)
    app.state.database_ready = True
    try:
        yield
    finally:
        app.state.database_ready = False


app = FastAPI(title="ImPRESS Print Works", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        # Chunked uploads use PUT; preflight must allow it for a cross-origin storefront.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
@app.middleware("http")
async def revalidate_pages_and_code(request: Request, call_next):
    """Make browsers re-check HTML, JavaScript, and CSS on every load.

    Without this, a browser may keep running an old cached script against a newer page after an
    update. Unchanged files are still answered with a cheap 304 Not Modified.
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "Cache-Control" not in response.headers and content_type.startswith(("text/html", "text/javascript", "application/javascript", "text/css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Inline proof/draft uploads are JSON data URLs, which are about a third larger than the file itself.
# Reject oversized bodies before FastAPI parses them; chunked uploads use a different streaming endpoint.
INLINE_FILE_ROUTES = (
    re.compile(r"^/api/orders/[^/]+/payments$"),
    re.compile(r"^/api/orders/[^/]+/payments/scan$"),
    re.compile(r"^/api/orders/[^/]+/refund-request$"),
    re.compile(r"^/api/orders/[^/]+/delivery-issue$"),
    re.compile(r"^/api/admin/orders/[^/]+/draft$"),
    re.compile(r"^/api/admin/orders/[^/]+/refunds/[^/]+/[^/]+$"),
)
INLINE_BODY_LIMIT = int(settings.max_upload_mb * 1024 * 1024 * 4 / 3) + 256 * 1024


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={"detail": f"The uploaded file is too large for this request. Use a file no larger than {settings.max_upload_mb} MB."},
    )


@app.middleware("http")
async def cap_inline_upload_bodies(request: Request, call_next):
    """Cap the real body bytes, including chunked-transfer requests without Content-Length.

    The bounded body is replayed to the route so FastAPI can parse it normally; oversized bodies
    never reach JSON parsing or the service's file decoders.
    """
    if request.method != "POST" or not any(pattern.match(request.url.path) for pattern in INLINE_FILE_ROUTES):
        return await call_next(request)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > INLINE_BODY_LIMIT:
        return _too_large_response()

    body = bytearray()
    stream = request.stream()
    try:
        async for chunk in stream:
            body.extend(chunk)
            if len(body) > INLINE_BODY_LIMIT:
                return _too_large_response()
    except ClientDisconnect:
        return JSONResponse(status_code=400, content={"detail": "The request was interrupted."})
    finally:
        await stream.aclose()

    # BaseHTTPMiddleware replays a cached body to the route; store the bounded bytes here so
    # FastAPI still parses the original JSON without reading the network stream a second time.
    request._body = bytes(body)  # noqa: SLF001 - Starlette's documented body cache
    return await call_next(request)


app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
# Product images are stored in the catalog as /frontend/image/... paths.
app.mount("/frontend", StaticFiles(directory=ASSETS_DIR), name="frontend")
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(order_router)
app.include_router(operations_router)
app.include_router(uploads_router)
app.include_router(admin_router)
app.include_router(chatbot_router)


def _customer_page(filename: str):
    def handler() -> FileResponse:
        return FileResponse(CUSTOMER_DIR / filename)

    return handler


def _customer_order_page(filename: str):
    def handler(request: Request) -> Response:
        # The portal shell itself is public, but a direct per-order link is private. Preserve it through
        # sign-in so the customer returns to the same order instead of the account list.
        if session_claims(request, "customer") is None:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/sign-in?{urlencode({'next': target})}", status_code=303)
        return FileResponse(CUSTOMER_DIR / filename)

    return handler


def _admin_page(filename: str):
    def handler(request: Request) -> Response:
        if session_claims(request, "admin") is None:
            # Keep the requested admin page so signing in returns here instead of the Dashboard.
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/admin/login?{urlencode({'next': target})}", status_code=303)
        return FileResponse(ADMIN_DIR / filename)

    return handler


def _redirect(target: str):
    def handler() -> RedirectResponse:
        return RedirectResponse(target, status_code=303)

    return handler


def _admin_login() -> FileResponse:
    return FileResponse(ADMIN_DIR / "Login.html")


def _admin_forgot_password() -> FileResponse:
    return FileResponse(ADMIN_DIR / "ForgotPassword.html")


for _path, _filename in CUSTOMER_PAGES.items():
    app.add_api_route(_path, _customer_page(_filename), methods=["GET"])
for _path, _filename in ADMIN_PAGES.items():
    app.add_api_route(_path, _admin_page(_filename), methods=["GET"])
# Stable per-order URLs. Both shells read the reference from the path and restore the order from the
# database; they are added after the exact view routes so nothing existing is shadowed.
for _suffix in ("", "/payment", "/approval"):
    app.add_api_route(
        f"/customer/orders/{{reference}}{_suffix}",
        _customer_order_page("customer-portal.html"),
        methods=["GET"],
    )
for _suffix in ("", "/payments"):
    app.add_api_route(
        f"/admin/orders/{{reference}}{_suffix}",
        _admin_page("OrderManagement.html"),
        methods=["GET"],
    )
for _path, _target in REDIRECTS.items():
    app.add_api_route(_path, _redirect(_target), methods=["GET"])
for _path in ("/login", "/admin", "/admin/login"):
    app.add_api_route(_path, _admin_login, methods=["GET"])
app.add_api_route("/admin/forgot-password", _admin_forgot_password, methods=["GET"])

# The sign-in, registration, and password-reset forms use method="post", so a form sent before the page
# script has loaded keeps the email and password in the request body, never in the URL. The scripts
# send the real request as JSON to the /api/... endpoints. A plain form post is not processed (the body
# is never read): the browser is sent back to the same page, keeping its ?next= return address.
AUTH_FORM_PAGES = ("/sign-in", "/create-account", "/verify-email", "/forgot-password", "/login", "/admin", "/admin/login", "/admin/forgot-password")


def _reload_auth_page(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(target, status_code=303)


for _path in AUTH_FORM_PAGES:
    app.add_api_route(_path, _reload_auth_page, methods=["POST"], include_in_schema=False)


@app.get("/api/health")
def health_check(request: Request) -> dict[str, str]:
    if not getattr(request.app.state, "database_ready", False):
        raise HTTPException(status_code=503, detail="The database is not ready.")
    return {"status": "ok"}
