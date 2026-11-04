from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.code.config import settings
from backend.app.code.dependencies import require_local_admin
from backend.app.code.security import session_claims
from backend.app.database import customer_store
from backend.app.repositories import order_repository
from backend.app.routes.auth import router as auth_router
from backend.app.routes.order import router as order_router


PROJECT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_DIR / "frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"
CUSTOMER_DIR = FRONTEND_DIR / "customer"
ADMIN_DIR = FRONTEND_DIR / "admin"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    customer_store.initialize()
    order_repository.initialize()
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
app.mount("/frontend", StaticFiles(directory=ASSETS_DIR), name="frontend")
app.include_router(auth_router)
app.include_router(order_router)


def page(directory: Path, filename: str) -> FileResponse:
    return FileResponse(directory / filename)


def admin_page(filename: str, request: Request) -> Response:
    if session_claims(request, "admin") is None:
        return RedirectResponse("/admin/login", status_code=303)
    return page(ADMIN_DIR, filename)


@app.get("/")
@app.get("/home")
def read_home() -> FileResponse:
    return page(CUSTOMER_DIR, "index.html")


@app.get("/services")
def read_services() -> FileResponse:
    return page(CUSTOMER_DIR, "services.html")


@app.get("/order")
def read_order() -> FileResponse:
    return page(CUSTOMER_DIR, "order.html")


@app.get("/sample-work")
def read_sample_work() -> FileResponse:
    return page(CUSTOMER_DIR, "sample-work.html")


@app.get("/customer-portal")
def read_customer_portal() -> FileResponse:
    return page(CUSTOMER_DIR, "customer-portal.html")


@app.get("/account")
def read_customer_account() -> RedirectResponse:
    return RedirectResponse("/customer-portal", status_code=303)


@app.get("/finished-works")
def read_finished_works() -> FileResponse:
    return page(CUSTOMER_DIR, "finished-works.html")


@app.get("/login")
@app.get("/admin/login")
def read_admin_login() -> FileResponse:
    return page(ADMIN_DIR, "Login.html")


@app.get("/admin")
def read_admin_root() -> FileResponse:
    return page(ADMIN_DIR, "Login.html")


@app.get("/admin/dashboard")
def read_admin_dashboard(request: Request) -> Response:
    return admin_page("Dashboard.html", request)


@app.get("/admin/orders")
def read_admin_orders(request: Request) -> Response:
    return admin_page("OrderManagement.html", request)


@app.get("/admin/customers")
def read_admin_customers(request: Request) -> Response:
    return admin_page("CustomirManagement.html", request)


@app.get("/admin/services")
def read_admin_services(request: Request) -> Response:
    return admin_page("ServicesProducts.html", request)


@app.get("/admin/pricing")
def read_admin_pricing(request: Request) -> Response:
    return admin_page("PricingManagement.html", request)


@app.get("/admin/payments")
def read_admin_payments(request: Request) -> Response:
    return admin_page("PaymentMonitoring.html", request)


@app.get("/admin/reports")
def read_admin_reports(request: Request) -> Response:
    return admin_page("SalesReports.html", request)


@app.get("/admin/notifications")
def read_admin_notifications(request: Request) -> Response:
    return admin_page("NotificationManagement.html", request)


@app.get("/admin/settings")
def read_admin_settings(request: Request) -> Response:
    return admin_page("Settings.html", request)


@app.get("/api/health")
def health_check(request: Request) -> dict[str, str]:
    if not getattr(request.app.state, "database_ready", False):
        raise HTTPException(status_code=503, detail="The database is not ready.")
    return {"status": "ok"}


@app.get("/api/admin/customers")
def admin_customers(request: Request) -> dict[str, object]:
    require_local_admin(request)
    return {"customers": customer_store.list_customers()}
