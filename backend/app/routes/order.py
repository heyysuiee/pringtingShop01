from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import FileResponse

from backend.app.code.dependencies import require_local_admin
from backend.app.code.security import require_same_origin, require_session
from backend.app.schemas.order import (
    AdminDraft,
    AdminOrderStatus,
    AdminPayment,
    AdminQuotation,
    CustomerOrderAction,
    CustomerOrderChanges,
    OrderSubmission,
)
from backend.app.services import order_services


router = APIRouter(tags=["orders"])


@router.post("/api/orders", status_code=201)
def submit_order(
    payload: OrderSubmission,
    request: Request,
    response: Response,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    require_same_origin(request)
    result, was_replayed = order_services.submit_order(payload.model_dump(), request, idempotency_key)
    if was_replayed:
        response.status_code = 200
    return result


@router.get("/api/orders")
def customer_orders(email: str, reference: str) -> dict[str, Any]:
    return order_services.customer_orders(email, reference)


@router.get("/api/account/orders")
def account_orders(request: Request) -> dict[str, Any]:
    claims = require_session(request, "customer")
    return order_services.account_orders(claims["sub"])


@router.get("/api/orders/{reference}")
def customer_order(reference: str, email: str) -> dict[str, Any]:
    return order_services.get_customer_order(reference, email)


@router.get("/api/orders/{reference}/files/{file_id}")
def customer_order_file(reference: str, file_id: str, email: str) -> FileResponse:
    return order_services.get_customer_order_file(reference, file_id, email)


@router.post("/api/orders/{reference}/approve")
def approve_customer_draft(
    reference: str,
    payload: CustomerOrderAction,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    return order_services.approve_customer_draft(reference, payload.email)


@router.post("/api/orders/{reference}/request-changes")
def request_draft_changes(
    reference: str,
    payload: CustomerOrderChanges,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    return order_services.request_draft_changes(reference, payload.email, payload.note)


@router.post("/api/orders/{reference}/cancel")
def cancel_customer_order(
    reference: str,
    payload: CustomerOrderAction,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    return order_services.cancel_customer_order(reference, payload.email)


@router.get("/api/admin/orders")
def admin_orders(request: Request) -> dict[str, Any]:
    require_local_admin(request)
    return order_services.admin_orders()


@router.get("/api/admin/orders/{reference}/files/{file_id}")
def admin_order_file(reference: str, file_id: str, request: Request) -> FileResponse:
    require_local_admin(request)
    return order_services.get_admin_order_file(reference, file_id)


@router.post("/api/admin/orders/{reference}/quotation")
def admin_quote(
    reference: str,
    payload: AdminQuotation,
    request: Request,
) -> dict[str, Any]:
    require_local_admin(request)
    return order_services.set_quotation(reference, payload.model_dump())


@router.post("/api/admin/orders/{reference}/payments")
def admin_record_payment(
    reference: str,
    payload: AdminPayment,
    request: Request,
) -> dict[str, Any]:
    require_local_admin(request)
    return order_services.record_payment(reference, payload.model_dump())


@router.post("/api/admin/orders/{reference}/draft")
def admin_upload_draft(
    reference: str,
    payload: AdminDraft,
    request: Request,
) -> dict[str, Any]:
    require_local_admin(request)
    return order_services.upload_draft(reference, payload.model_dump())


@router.post("/api/admin/orders/{reference}/status")
def admin_update_status(
    reference: str,
    payload: AdminOrderStatus,
    request: Request,
) -> dict[str, Any]:
    require_local_admin(request)
    return order_services.update_order_status(reference, payload.status)
