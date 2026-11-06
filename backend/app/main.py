from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.core.config import settings
from backend.app.core.security import session_claims
from backend.app.repositories import catalog_repository, customer_repository, order_repository
from backend.app.routes.auth import router as auth_router
from backend.app.routes.catalog import router as catalog_router
from backend.app.routes.order import router as order_router


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
}
# Admin pages require an administrator session: URL path -> file in frontend/admin.
ADMIN_PAGES = {
    "/admin/dashboard": "Dashboard.html",
    "/admin/orders": "OrderManagement.html",
    "/admin/customers": "CustomerManagement.html",
    "/admin/services": "ServicesProducts.html",
    "/admin/pricing": "PricingManagement.html",
    "/admin/payments": "PaymentMonitoring.html",
    "/admin/reports": "SalesReports.html",
    "/admin/notifications": "NotificationManagement.html",
    "/admin/settings": "Settings.html",
}
# Old or alternative URLs kept working.
REDIRECTS = {
    "/account": "/customer-portal",
    "/finished-works": "/sample-work",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    customer_repository.initialize()
    order_repository.initialize()
    catalog_repository.initialize()
    app.state.database_ready = True
    try:
        yield
    finally:
        app.state.database_ready = False


app = FastAPI(title="ImPRESSion PrintShop", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
# Product images are stored in the catalog as /frontend/image/... paths.
app.mount("/frontend", StaticFiles(directory=ASSETS_DIR), name="frontend")
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(order_router)


def _customer_page(filename: str):
    def handler() -> FileResponse:
        return FileResponse(CUSTOMER_DIR / filename)

    return handler


def _admin_page(filename: str):
    def handler(request: Request) -> Response:
        if session_claims(request, "admin") is None:
            return RedirectResponse("/admin/login", status_code=303)
        return FileResponse(ADMIN_DIR / filename)

    return handler


def _redirect(target: str):
    def handler() -> RedirectResponse:
        return RedirectResponse(target, status_code=303)

    return handler


def _admin_login() -> FileResponse:
    return FileResponse(ADMIN_DIR / "Login.html")


for _path, _filename in CUSTOMER_PAGES.items():
    app.add_api_route(_path, _customer_page(_filename), methods=["GET"])
for _path, _filename in ADMIN_PAGES.items():
    app.add_api_route(_path, _admin_page(_filename), methods=["GET"])
for _path, _target in REDIRECTS.items():
    app.add_api_route(_path, _redirect(_target), methods=["GET"])
for _path in ("/login", "/admin", "/admin/login"):
    app.add_api_route(_path, _admin_login, methods=["GET"])


@app.get("/api/health")
def health_check(request: Request) -> dict[str, str]:
    if not getattr(request.app.state, "database_ready", False):
        raise HTTPException(status_code=503, detail="The database is not ready.")
    return {"status": "ok"}
