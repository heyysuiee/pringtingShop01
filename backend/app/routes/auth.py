from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from backend.app.core.config import settings
from backend.app.core.dependencies import require_admin
from backend.app.core.rate_limit import (
    LOGIN_LIMIT,
    OTP_REQUEST_LIMIT,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_VERIFY_LIMIT,
    RESET_CONFIRM_LIMIT,
    limiter,
)
from backend.app.core.security import (
    clear_session_cookie,
    create_session_cookie,
    require_same_origin,
    require_session,
    verify_password,
)
from backend.app.schemas.user import (
    CustomerLogin,
    CustomerProfileUpdate,
    CustomerRegistration,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetVerify,
)
from backend.app.services import auth_services


router = APIRouter(tags=["authentication"])


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttle(bucket: str, key: str, limit: tuple[int, int], detail: str) -> None:
    max_requests, window = limit
    if not limiter.allow(bucket, key, max_requests, window):
        raise HTTPException(status_code=429, detail=detail)


@router.post("/api/customer/register", status_code=201)
def register_customer(payload: CustomerRegistration, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    _throttle("register", _client_address(request), (20, 60 * 60), "Too many registration attempts. Try again later.")
    try:
        customer = auth_services.register_customer(payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_session_cookie(response, request, customer["id"], "customer")
    return {"customer": customer}


@router.post("/api/customer/session")
def login_customer(payload: CustomerLogin, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    _throttle("login-ip", _client_address(request), LOGIN_LIMIT, "Too many sign-in attempts. Try again in a few minutes.")
    _throttle("login-email", payload.email, LOGIN_LIMIT, "Too many sign-in attempts for this account. Try again in a few minutes.")
    customer = auth_services.authenticate_customer(payload.email, payload.password)
    if customer is None:
        raise HTTPException(status_code=401, detail="Email address or password is incorrect.")
    create_session_cookie(response, request, customer["id"], "customer")
    return {"customer": customer}


@router.get("/api/customer/session")
def customer_session(request: Request) -> dict[str, object]:
    claims = require_session(request, "customer")
    customer = auth_services.get_customer(claims["sub"])
    if customer is None:
        raise HTTPException(status_code=401, detail="The customer account is no longer available.")
    return {"customer": customer}


@router.delete("/api/customer/session", status_code=204)
def logout_customer(request: Request, response: Response) -> Response:
    require_same_origin(request)
    clear_session_cookie(response)
    response.status_code = 204
    return response


# ---- Forgot password: email OTP --------------------------------------------------

@router.post("/api/customer/password-reset/request")
def request_password_reset(payload: PasswordResetRequest, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    _throttle("otp-request-ip", _client_address(request), OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.")
    # Cooldown is keyed by the submitted email, so the same response is returned for known and unknown addresses.
    cooled_down = not limiter.allow("otp-cooldown", payload.email, 1, OTP_RESEND_COOLDOWN_SECONDS)
    _throttle("otp-request-email", payload.email, OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.")
    if cooled_down:
        return {"message": auth_services.GENERIC_REQUEST_MESSAGE, "resend_after": OTP_RESEND_COOLDOWN_SECONDS}
    result = auth_services.request_password_reset(payload.email)
    result["resend_after"] = OTP_RESEND_COOLDOWN_SECONDS
    return result


@router.post("/api/customer/password-reset/verify")
def verify_password_reset(payload: PasswordResetVerify, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("otp-verify-ip", _client_address(request), OTP_VERIFY_LIMIT, "Too many verification attempts. Try again later.")
    token = auth_services.verify_password_reset_code(payload.email, payload.code)
    return {"reset_token": token, "message": "Code verified. Choose your new password."}


@router.post("/api/customer/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("reset-confirm-ip", _client_address(request), RESET_CONFIRM_LIMIT, "Too many attempts. Try again later.")
    auth_services.complete_password_reset(payload.email, payload.reset_token, payload.new_password)
    return {"message": "Your password has been changed. You can now sign in with your new password."}


# ---- Profile -----------------------------------------------------------------------

@router.get("/api/account/profile")
def read_customer_profile(request: Request) -> dict[str, object]:
    claims = require_session(request, "customer")
    customer = auth_services.get_customer(claims["sub"])
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer account not found.")
    return {"customer": customer}


@router.patch("/api/account/profile")
def update_customer_profile(payload: CustomerProfileUpdate, request: Request) -> dict[str, object]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    customer = auth_services.update_customer(claims["sub"], payload.model_dump())
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer account not found.")
    return {"customer": customer}


# ---- Administrator session ---------------------------------------------------------

@router.post("/api/admin/session")
def create_admin_session(payload: CustomerLogin, request: Request, response: Response) -> dict[str, str]:
    require_same_origin(request)
    if not settings.admin_email or not settings.admin_password_hash:
        raise HTTPException(status_code=503, detail="Administrator credentials have not been configured.")
    _throttle("admin-login-ip", _client_address(request), LOGIN_LIMIT, "Too many sign-in attempts. Try again in a few minutes.")
    if payload.email != settings.admin_email or not verify_password(payload.password, settings.admin_password_hash):
        raise HTTPException(status_code=401, detail="Administrator credentials are incorrect.")
    create_session_cookie(response, request, settings.admin_email, "admin")
    return {"mode": "authenticated"}


@router.get("/api/admin/session")
def admin_session(request: Request) -> dict[str, str]:
    claims = require_session(request, "admin")
    return {"mode": "authenticated", "email": claims["sub"]}


@router.delete("/api/admin/session", status_code=204)
def delete_admin_session(request: Request, response: Response) -> Response:
    require_same_origin(request)
    clear_session_cookie(response)
    response.status_code = 204
    return response


@router.get("/api/admin/customers")
def admin_customers(request: Request) -> dict[str, object]:
    require_admin(request)
    return {"customers": auth_services.list_customers()}
