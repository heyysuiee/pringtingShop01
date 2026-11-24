"""Administrator-only reporting, customer, payment, activity, and export endpoints.

Every endpoint requires an administrator session. Filters, sorting, and pagination are passed as
query parameters and applied on the server.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.app.core.dependencies import require_admin
from backend.app.services import backup_services, export_services, report_services


router = APIRouter(tags=["admin"])


def _params(request: Request) -> dict[str, Any]:
    require_admin(request)
    return dict(request.query_params)


@router.get("/api/admin/counts")
def action_counts(request: Request) -> dict[str, Any]:
    """Numbers for the sidebar notification badges (records that need an administrator's action)."""
    require_admin(request)
    return report_services.action_counts()


@router.get("/api/admin/customers")
def admin_customers(request: Request) -> dict[str, Any]:
    return report_services.customer_list(_params(request))


@router.get("/api/admin/customers/{customer_id}")
def admin_customer(customer_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return report_services.customer_detail(customer_id)


@router.get("/api/admin/payments")
def admin_payments(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return report_services.payment_list(_params(request))


@router.get("/api/admin/reports/completed")
def completed_report(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return report_services.completed_list(_params(request))


@router.get("/api/admin/reports/services")
def service_report(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return report_services.service_report(_params(request))


@router.get("/api/admin/reports/summary")
def business_summary(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return report_services.business_summary(_params(request))


@router.get("/api/admin/activity")
def activity(request: Request) -> dict[str, Any]:
    return report_services.activity_feed(_params(request))


@router.get("/api/admin/exports/{kind}")
def export_report(kind: str, request: Request) -> Response:
    """Download a report as ``format=pdf`` or ``format=xlsx`` using the same filters as the screen."""
    return export_services.export(kind, _params(request))


@router.get("/api/admin/customers-report")
def customer_print_report(request: Request) -> dict[str, Any]:
    """Data for the printable customer report: the screen's view and filters, or one ``customer_id``."""
    return report_services.customer_print_report(_params(request))


# ---- Notifications (customer activity) ------------------------------------------------


@router.get("/api/admin/notifications")
def notifications(request: Request, response: Response) -> dict[str, Any]:
    """The Notifications page: filters, search, and pages; each item has its read state."""
    response.headers["Cache-Control"] = "no-store"
    return report_services.notification_list(_params(request))


@router.get("/api/admin/notifications/recent")
def recent_notifications(request: Request, response: Response) -> dict[str, Any]:
    """The header bell dropdown: latest customer notifications and the unread count."""
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return report_services.recent_notifications()


@router.post("/api/admin/notifications/read")
async def read_notifications(request: Request) -> dict[str, Any]:
    """Mark notifications read: ``{"ids": [...]}``, ``{"all": true}``, or ``{"before": "<ISO time>"}``."""
    require_admin(request)
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid request.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid request.")
    return report_services.mark_notifications_read(payload.get("ids"), payload.get("all") is True, payload.get("before"))


# ---- Database backup and restore --------------------------------------------------------


@router.get("/api/admin/backup")
def backup_status(request: Request, response: Response) -> dict[str, Any]:
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return backup_services.status()


@router.post("/api/admin/backup")
def create_backup(request: Request) -> Response:
    """Create a backup of the shop's data and download it as a JSON file."""
    admin = require_admin(request)
    content, filename, event = backup_services.create_backup(admin["sub"])
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Backup-Rows": str(event["rows"]),
        },
    )


@router.post("/api/admin/backup/restore")
async def restore_backup(request: Request) -> dict[str, Any]:
    """Restore a backup file created by this system.

    Body: ``{"confirmation": "RESTORE", "backup_text": "<the file's text>"}`` (or ``"backup": {...}``).
    """
    admin = require_admin(request)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > backup_services.MAX_BACKUP_BYTES + 1024:
        raise HTTPException(status_code=413, detail=f"The backup file is too large (limit {backup_services.MAX_BACKUP_BYTES // (1024 * 1024)} MB).")
    body = await request.body()
    if len(body) > backup_services.MAX_BACKUP_BYTES + 1024:
        raise HTTPException(status_code=413, detail=f"The backup file is too large (limit {backup_services.MAX_BACKUP_BYTES // (1024 * 1024)} MB).")
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="This backup file cannot be restored: it is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid restore request.")
    document = payload.get("backup")
    if isinstance(payload.get("backup_text"), str):
        # The file's exact text, as uploaded (preferred: nothing is re-encoded on the way).
        try:
            document = json.loads(payload["backup_text"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail="This backup file cannot be restored: it is not valid JSON.") from error
    return backup_services.restore_backup(document, payload.get("confirmation"), admin["sub"])
