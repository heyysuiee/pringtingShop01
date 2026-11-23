"""Payment methods, fulfillment settings, production monitoring, pickup and delivery, cancellations, and refunds.

Customer endpoints need the signed-in account that owns the order; administrator endpoints need an
administrator session. Every rule (amounts, eligibility, allowed status moves) is enforced by the services.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from backend.app.core.dependencies import require_admin
from backend.app.core.security import require_same_origin, require_session
from backend.app.services import fulfillment_services, location_services, refund_services, shop_settings_services


router = APIRouter(tags=["operations"])


def _body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid request.")
    return payload


def _admin_body(payload: Any) -> dict[str, Any]:
    """An admin order action always carries the order version shown on screen.

    Customer actions do not have a version to send. Raw-body admin endpoints have no Pydantic schema,
    so the same positive-integer rule is enforced here before a service can save anything.
    """
    body = _body(payload)
    version = body.get("expected_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HTTPException(status_code=422, detail="Order version must be a positive whole number.")
    return body


def admin_claims(request: Request) -> dict[str, Any]:
    """Authenticate before body parsing so unauthenticated admin writes still answer 401, not 422."""
    return require_admin(request)


# ================================================================== public


@router.get("/api/payment-methods")
def payment_methods() -> dict[str, Any]:
    """Active e-wallet options with the shop's account details and QR codes."""
    return shop_settings_services.public_payment_methods()


@router.get("/api/payment-methods/{method_id}/qr")
def payment_method_qr(method_id: str) -> Response:
    return shop_settings_services.qr_response(method_id)


@router.get("/api/fulfillment-options")
def fulfillment_options() -> dict[str, Any]:
    """Delivery areas (fee and estimated courier period), pickup locations, and instructions."""
    return shop_settings_services.public_fulfillment_options()


@router.get("/api/locations")
def locations() -> dict[str, Any]:
    """Read-only PSGC provinces, their cities/municipalities, and island groups for the address form."""
    return location_services.public_locations()


@router.get("/api/shipping-quote")
def shipping_quote(province: str = "", city: str = "", area_id: str | None = None) -> dict[str, Any]:
    """Server-calculated shipping fee and estimate for the address currently selected at checkout."""
    return shop_settings_services.shipping_quote(province, city, area_id)


# ================================================================== customer actions


def _customer(request: Request) -> str:
    require_same_origin(request)
    return require_session(request, "customer")["sub"]


@router.post("/api/orders/{reference}/cancellation-request")
def request_cancellation(reference: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    return refund_services.request_cancellation(reference, _body(payload), _customer(request))


@router.post("/api/orders/{reference}/refund-request")
def request_refund(reference: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    return refund_services.request_refund(reference, _body(payload), _customer(request))


@router.post("/api/orders/{reference}/delivery-issue")
def report_delivery_issue(reference: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    return fulfillment_services.report_issue(reference, _body(payload), _customer(request))


# ================================================================== admin lists


@router.get("/api/admin/production")
def production_list(request: Request) -> dict[str, Any]:
    require_admin(request)
    return fulfillment_services.admin_production(dict(request.query_params))


@router.get("/api/admin/fulfillment")
def fulfillment_list(request: Request) -> dict[str, Any]:
    require_admin(request)
    return fulfillment_services.admin_fulfillment(dict(request.query_params))


@router.get("/api/admin/cancellations")
def cancellation_list(request: Request) -> dict[str, Any]:
    require_admin(request)
    return refund_services.admin_cancellations(dict(request.query_params))


@router.get("/api/admin/refunds")
def refund_list(request: Request) -> dict[str, Any]:
    require_admin(request)
    return refund_services.admin_refunds(dict(request.query_params))


# ================================================================== admin order actions

ORDER_ACTIONS = {
    "production": fulfillment_services.update_production,
    "quality-check": fulfillment_services.record_quality_check,
    "packaging": fulfillment_services.complete_packaging,
    "dispatch": fulfillment_services.dispatch_order,
    "delivery/delivered": fulfillment_services.confirm_delivery,
    "delivery/failed": fulfillment_services.delivery_failed,
    "delivery/reschedule": fulfillment_services.reschedule_delivery,
    "pickup/schedule": fulfillment_services.schedule_pickup,
    "pickup/confirm": fulfillment_services.confirm_pickup,
    "refunds": refund_services.create_refund,
    "reprint": fulfillment_services.start_reprint,
}


def _order_action(action: str):
    handler = ORDER_ACTIONS[action]

    def endpoint(
        reference: str,
        request: Request,
        payload: Any = Body(default=None),
        admin: dict[str, Any] = Depends(admin_claims),
    ) -> dict[str, Any]:
        return handler(reference, _admin_body(payload), actor=admin["sub"])

    endpoint.__name__ = f"admin_{action.replace('/', '_').replace('-', '_')}"
    return endpoint


for _action in ORDER_ACTIONS:
    router.add_api_route(f"/api/admin/orders/{{reference}}/{_action}", _order_action(_action), methods=["POST"])


@router.post("/api/admin/orders/{reference}/issues/{issue_id}/resolve")
def resolve_issue(
    reference: str,
    issue_id: str,
    request: Request,
    payload: Any = Body(default=None),
    admin: dict[str, Any] = Depends(admin_claims),
) -> dict[str, Any]:
    return fulfillment_services.resolve_issue(reference, issue_id, _admin_body(payload), actor=admin["sub"])


@router.post("/api/admin/orders/{reference}/cancellations/{request_id}/{decision}")
def decide_cancellation(
    reference: str,
    request_id: str,
    decision: str,
    request: Request,
    payload: Any = Body(default=None),
    admin: dict[str, Any] = Depends(admin_claims),
) -> dict[str, Any]:
    if decision == "approve":
        return refund_services.approve_cancellation(reference, request_id, _admin_body(payload), actor=admin["sub"])
    if decision == "reject":
        return refund_services.reject_cancellation(reference, request_id, _admin_body(payload), actor=admin["sub"])
    raise HTTPException(status_code=404, detail="Not found.")


@router.post("/api/admin/orders/{reference}/refunds/{refund_id}/{action}")
def refund_action(
    reference: str,
    refund_id: str,
    action: str,
    request: Request,
    payload: Any = Body(default=None),
    admin: dict[str, Any] = Depends(admin_claims),
) -> dict[str, Any]:
    body, actor = _admin_body(payload), admin["sub"]
    if action == "approve":
        return refund_services.approve_refund(reference, refund_id, body, actor=actor)
    if action == "reject":
        return refund_services.decline_refund(reference, refund_id, body, "rejected", actor=actor)
    if action == "not-eligible":
        return refund_services.decline_refund(reference, refund_id, body, "not_eligible", actor=actor)
    if action == "processing":
        return refund_services.start_processing(reference, refund_id, body, actor=actor)
    if action == "complete":
        return refund_services.complete_refund(reference, refund_id, body, actor=actor)
    if action == "fail":
        return refund_services.fail_refund(reference, refund_id, body, actor=actor)
    raise HTTPException(status_code=404, detail="Not found.")


# ================================================================== admin settings


@router.get("/api/admin/settings/payment-methods")
def admin_payment_methods(request: Request, response: Response) -> dict[str, Any]:
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return shop_settings_services.admin_payment_methods()


@router.post("/api/admin/settings/payment-methods", status_code=201)
def create_payment_method(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    require_admin(request)
    return shop_settings_services.create_payment_method(_body(payload))


@router.patch("/api/admin/settings/payment-methods/{method_id}")
def update_payment_method(method_id: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    require_admin(request)
    return shop_settings_services.update_payment_method(method_id, _body(payload))


@router.delete("/api/admin/settings/payment-methods/{method_id}")
def delete_payment_method(method_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return shop_settings_services.delete_payment_method(method_id)


@router.get("/api/admin/settings/fulfillment")
def admin_fulfillment_settings(request: Request, response: Response) -> dict[str, Any]:
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return shop_settings_services.fulfillment_settings()


@router.post("/api/admin/settings/fulfillment")
def save_fulfillment_settings(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
    require_admin(request)
    return shop_settings_services.save_fulfillment_settings(_body(payload))
