from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response

from backend.app.core.dependencies import require_admin
from backend.app.core.security import require_same_origin, require_session
from backend.app.schemas.order import (
    AdminConfirmation,
    AdminDraft,
    AdminOrderStatus,
    AdminPayment,
    AdminPaymentOption,
    AdminPaymentReference,
    AdminQuotation,
    AdminSubmissionReview,
    CustomerOrderAction,
    CustomerOrderChanges,
    CustomerPaymentSubmission,
    CustomerQuotationResponse,
    NotificationsRead,
    OrderSubmission,
)
from backend.app.services import notification_services, order_services


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
def customer_orders(request: Request) -> dict[str, Any]:
    """The signed-in customer's orders (same as /api/account/orders). An email and reference no longer grant access."""
    claims = require_session(request, "customer")
    return order_services.account_orders(claims["sub"])


@router.get("/api/account/orders")
def account_orders(request: Request) -> dict[str, Any]:
    claims = require_session(request, "customer")
    return order_services.account_orders(claims["sub"])


@router.get("/api/account/orders/{reference}")
def account_order(reference: str, request: Request) -> dict[str, Any]:
    claims = require_session(request, "customer")
    return order_services.account_order(claims["sub"], reference)


@router.get("/api/orders/{reference}")
def customer_order(reference: str, request: Request) -> dict[str, Any]:
    claims = require_session(request, "customer")
    return order_services.account_order(claims["sub"], reference)


@router.get("/api/orders/{reference}/files/{file_id}")
def customer_order_file(reference: str, file_id: str, request: Request) -> Response:
    # Only the signed-in customer who owns the order; any old "?email=" in a link is ignored.
    claims = require_session(request, "customer")
    return order_services.get_customer_order_file(claims["sub"], reference, file_id)


@router.post("/api/orders/{reference}/approve")
def approve_customer_draft(
    reference: str,
    payload: CustomerOrderAction,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.approve_customer_draft(reference, payload.email, claims["sub"])


@router.post("/api/orders/{reference}/request-changes")
def request_draft_changes(
    reference: str,
    payload: CustomerOrderChanges,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.request_draft_changes(reference, payload.email, payload.note, claims["sub"])


@router.post("/api/orders/{reference}/cancel")
def cancel_customer_order(
    reference: str,
    payload: CustomerOrderAction,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.cancel_customer_order(reference, payload.email, claims["sub"])


@router.post("/api/orders/{reference}/quotation/accept")
def accept_quotation(
    reference: str,
    payload: CustomerQuotationResponse,
    request: Request,
) -> dict[str, Any]:
    """The signed-in customer accepts the latest quotation; the order then waits for the 50% down payment."""
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.accept_quotation(reference, payload.email, payload.model_dump(), claims["sub"])


@router.post("/api/orders/{reference}/quotation/decline")
def decline_quotation(
    reference: str,
    payload: CustomerQuotationResponse,
    request: Request,
) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.decline_quotation(reference, payload.email, payload.model_dump(), claims["sub"])


@router.post("/api/orders/{reference}/payments")
def submit_customer_payment(
    reference: str,
    payload: CustomerPaymentSubmission,
    request: Request,
) -> dict[str, Any]:
    """The signed-in customer submits a GCash payment (reference + screenshot); the administrator verifies it."""
    require_same_origin(request)
    claims = require_session(request, "customer")
    return order_services.submit_customer_payment(reference, payload.email, payload.model_dump(), claims["sub"])


@router.get("/api/admin/orders")
def admin_orders(request: Request) -> dict[str, Any]:
    """Orders with server-side search, filters (status, service, payment status, dates), sorting, and pages."""
    require_admin(request)
    return order_services.admin_orders(dict(request.query_params))


@router.get("/api/admin/orders/{reference}")
def admin_order(reference: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return order_services.admin_order(reference)


@router.get("/api/admin/dashboard")
def admin_dashboard(request: Request) -> dict[str, Any]:
    require_admin(request)
    return order_services.admin_dashboard()


@router.get("/api/admin/orders/{reference}/audit")
def admin_order_audit(reference: str, request: Request) -> dict[str, Any]:
    """The order's audit trail (who changed what, when) and the notifications sent to the customer."""
    require_admin(request)
    return order_services.admin_order_audit(reference)


@router.get("/api/account/notifications")
def account_notifications(request: Request, response: Response) -> dict[str, Any]:
    claims = require_session(request, "customer")
    response.headers["Cache-Control"] = "no-store"
    return notification_services.customer_notifications(claims["sub"])


@router.post("/api/account/notifications/read")
def read_account_notifications(payload: NotificationsRead, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    return notification_services.mark_read(claims["sub"], payload.ids)


@router.get("/api/admin/orders/{reference}/files/{file_id}")
def admin_order_file(reference: str, file_id: str, request: Request) -> Response:
    require_admin(request)
    return order_services.get_admin_order_file(reference, file_id)


@router.post("/api/admin/orders/{reference}/quotation")
def admin_quote(
    reference: str,
    payload: AdminQuotation,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.set_quotation(reference, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/confirm")
def admin_confirm_package(
    reference: str,
    payload: AdminConfirmation,
    request: Request,
) -> dict[str, Any]:
    """Approve a fixed-price package order at its published total and request the 50% down payment.

    The order becomes Confirmed only when its down payment is verified.
    """
    admin = require_admin(request)
    return order_services.confirm_package_order(reference, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/payment-submissions/{submission_id}/verify")
def admin_verify_submission(
    reference: str,
    submission_id: str,
    payload: AdminSubmissionReview,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.verify_payment_submission(reference, submission_id, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/payment-submissions/{submission_id}/reject")
def admin_reject_submission(
    reference: str,
    submission_id: str,
    payload: AdminSubmissionReview,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.reject_payment_submission(reference, submission_id, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/payments")
def admin_record_payment(
    reference: str,
    payload: AdminPayment,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.record_payment(reference, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/payments/{payment_number}/reference")
def admin_add_payment_reference(
    reference: str,
    payment_number: int,
    payload: AdminPaymentReference,
    request: Request,
) -> dict[str, Any]:
    """Add the missing receipt or transaction number to an older payment recorded without one."""
    admin = require_admin(request)
    return order_services.add_payment_reference(reference, payment_number, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/payment-option")
def admin_update_payment_option(
    reference: str,
    payload: AdminPaymentOption,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.update_payment_option(reference, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/draft")
def admin_upload_draft(
    reference: str,
    payload: AdminDraft,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.upload_draft(reference, payload.model_dump(), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/status")
def admin_update_status(
    reference: str,
    payload: AdminOrderStatus,
    request: Request,
) -> dict[str, Any]:
    admin = require_admin(request)
    return order_services.update_order_status(reference, payload.status, payload.model_dump(), actor=admin["sub"])
