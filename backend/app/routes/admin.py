"""Administrator-only reporting, customer, payment, activity, and export endpoints.

Every endpoint requires an administrator session. Filters, sorting, and pagination are passed as
query parameters and applied on the server.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backend.app.core.dependencies import require_admin
from backend.app.services import export_services, report_services


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
def admin_payments(request: Request) -> dict[str, Any]:
    return report_services.payment_list(_params(request))


@router.get("/api/admin/reports/completed")
def completed_report(request: Request) -> dict[str, Any]:
    return report_services.completed_list(_params(request))


@router.get("/api/admin/reports/services")
def service_report(request: Request) -> dict[str, Any]:
    return report_services.service_report(_params(request))


@router.get("/api/admin/reports/summary")
def business_summary(request: Request) -> dict[str, Any]:
    return report_services.business_summary(_params(request))


@router.get("/api/admin/activity")
def activity(request: Request) -> dict[str, Any]:
    return report_services.activity_feed(_params(request))


@router.get("/api/admin/exports/{kind}")
def export_report(kind: str, request: Request) -> Response:
    """Download a report as ``format=pdf`` or ``format=xlsx`` using the same filters as the screen."""
    return export_services.export(kind, _params(request))
