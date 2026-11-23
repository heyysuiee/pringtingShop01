from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from backend.app.core.config import settings
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
)
from backend.app.schemas.user import (
    CustomerDeliveryAddress,
    CustomerLogin,
    CustomerProfileUpdate,
    CustomerRegistration,
    EmailVerificationRequest,
    EmailVerificationVerify,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetVerify,
)
from backend.app.repositories import auth_throttle_repository as otp_store
from backend.app.services import admin_auth_services, auth_services


router = APIRouter(tags=["authentication"])


def _client_address(request: Request) -> str | None:
    # Forwarded headers are trusted only from Vercel (which overwrites X-Real-IP itself) or from an
    # explicitly configured proxy. Otherwise a client-supplied X-Forwarded-For must never become a
    # rate-limit key: an attacker could rotate IP buckets at will. When untrusted we return None so
    # only the IP bucket is skipped; per-account database buckets remain authoritative.
    host = request.client.host if request.client else None
    if settings.behind_vercel_proxy:
        return request.headers.get("x-real-ip", "").strip() or host
    if host and host in settings.trusted_proxy_ips:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        hops = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        # The right-most hop is the address the trusted proxy actually saw.
        return hops[-1] if hops else host
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return None
    return host


def _auth_throttle_store() -> Any:
    """Production uses the shared database store so limits hold across restarts and instances."""
    return otp_store if settings.production else limiter


def _throttle(bucket: str, key: str | None, limit: tuple[int, int], detail: str, store: Any = limiter) -> None:
    """Raise 429 when the limit is reached; a missing/untrusted key skips only that bucket."""
    if not key:
        return
    max_requests, window = limit
    if not store.allow(bucket, key, max_requests, window):
        raise HTTPException(status_code=429, detail=detail)


@router.post("/api/customer/register", status_code=201)
def register_customer(payload: CustomerRegistration, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    store = _auth_throttle_store()
    _throttle("register-ip", _client_address(request), (20, 60 * 60), "Too many registration attempts. Try again later.", store)
    _throttle("register-email", payload.email, (20, 60 * 60), "Too many registration attempts. Try again later.", store)
    try:
        customer = auth_services.register_customer(payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    verification = customer.pop("email_verification", None)
    # With verification enabled, no session is issued until the code sent to the address is accepted.
    if verification is None:
        create_session_cookie(response, request, customer["id"], "customer")
    body: dict[str, object] = {"customer": customer}
    if verification is not None:
        # The registration code already started the 60-second resend window.
        otp_store.allow("email-verify-cooldown", payload.email, 1, OTP_RESEND_COOLDOWN_SECONDS)
        body["email_verification"] = verification
    return body


@router.post("/api/customer/session")
def login_customer(payload: CustomerLogin, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    store = _auth_throttle_store()
    _throttle("login-ip", _client_address(request), LOGIN_LIMIT, "Too many sign-in attempts. Try again in a few minutes.", store)
    _throttle("login-email", payload.email, LOGIN_LIMIT, "Too many sign-in attempts for this account. Try again in a few minutes.", store)
    customer = auth_services.authenticate_customer(payload.email, payload.password)
    if customer is None:
        raise HTTPException(status_code=401, detail="Email address or password is incorrect.")
    if settings.require_email_verification and not customer.get("email_verified", True):
        raise HTTPException(
            status_code=403,
            detail="Verify your email address before signing in. Use the code we sent you, or request a new one.",
        )
    create_session_cookie(response, request, customer["id"], "customer", remember=payload.remember)
    return {"customer": customer}


# ---- Registration email ownership ------------------------------------------------

@router.post("/api/customer/email-verification/request")
def request_email_verification(payload: EmailVerificationRequest, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    store = _auth_throttle_store()
    _throttle("email-verify-ip", _client_address(request), OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", store)
    _throttle("email-verify-email", payload.email, OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", store)
    cooled_down = not otp_store.allow("email-verify-cooldown", payload.email, 1, OTP_RESEND_COOLDOWN_SECONDS)
    if cooled_down:
        return {
            "message": "A verification code was requested for this address less than a minute ago. Use that code, or wait to request a new one.",
            "resend_after": OTP_RESEND_COOLDOWN_SECONDS,
            "cooldown": True,
        }
    try:
        result = auth_services.request_email_verification(payload.email)
    except HTTPException:
        otp_store.reset("email-verify-cooldown", payload.email)
        raise
    result["resend_after"] = OTP_RESEND_COOLDOWN_SECONDS
    return result


@router.post("/api/customer/email-verification/verify")
def verify_email_verification(payload: EmailVerificationVerify, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    _throttle(
        "email-verify-attempt-ip", _client_address(request), OTP_VERIFY_LIMIT,
        "Too many verification attempts. Try again later.", otp_store,
    )
    _throttle(
        "email-verify-attempt-email", payload.email, OTP_VERIFY_LIMIT,
        "Too many verification attempts. Try again later.", otp_store,
    )
    return auth_services.verify_email_address(payload.email, payload.code)


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
    clear_session_cookie(response, request)
    response.status_code = 204
    return response


# ---- Forgot password: email OTP --------------------------------------------------

@router.post("/api/customer/password-reset/request")
def request_password_reset(payload: PasswordResetRequest, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    if not settings.smtp_configured:
        # Checked before looking up the account, so every address gets the same answer.
        auth_services.log_missing_smtp_settings()
        raise HTTPException(status_code=503, detail="Password reset emails are not available right now. Please contact the shop.")
    _throttle("otp-request-ip", _client_address(request), OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", otp_store)
    # Cooldown is keyed by the submitted email, so the same response is returned for known and unknown addresses.
    # These OTP limits live in the database, so restarting the server does not reset them.
    cooled_down = not otp_store.allow("otp-cooldown", payload.email, 1, OTP_RESEND_COOLDOWN_SECONDS)
    _throttle("otp-request-email", payload.email, OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", otp_store)
    if cooled_down:
        # No new code is sent; the code from the previous request stays valid until it expires.
        return {
            "message": "A code was requested for this address less than a minute ago. Use the code from that email, or wait to request a new one.",
            "resend_after": OTP_RESEND_COOLDOWN_SECONDS,
            "cooldown": True,
        }
    try:
        result = auth_services.request_password_reset(payload.email)
    except HTTPException:
        # No email went out, so give the cooldown back: the customer may retry straight away
        # instead of being told to use a code that was never sent.
        otp_store.reset("otp-cooldown", payload.email)
        raise
    result["resend_after"] = OTP_RESEND_COOLDOWN_SECONDS
    return result


@router.post("/api/customer/password-reset/verify")
def verify_password_reset(payload: PasswordResetVerify, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("otp-verify-ip", _client_address(request), OTP_VERIFY_LIMIT, "Too many verification attempts. Try again later.", otp_store)
    _throttle("otp-verify-email", payload.email, OTP_VERIFY_LIMIT, "Too many verification attempts. Try again later.", otp_store)
    token = auth_services.verify_password_reset_code(payload.email, payload.code)
    return {"reset_token": token, "message": "Code verified. Choose your new password."}


@router.post("/api/customer/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("reset-confirm-ip", _client_address(request), RESET_CONFIRM_LIMIT, "Too many attempts. Try again later.", otp_store)
    _throttle("reset-confirm-email", payload.email, RESET_CONFIRM_LIMIT, "Too many attempts. Try again later.", otp_store)
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


@router.patch("/api/account/delivery-address")
def update_customer_delivery_address(payload: CustomerDeliveryAddress, request: Request) -> dict[str, object]:
    """Save the address used to prefill checkout; the order still recalculates and snapshots shipping."""
    require_same_origin(request)
    claims = require_session(request, "customer")
    customer = auth_services.save_delivery_address(claims["sub"], payload.model_dump())
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer account not found.")
    return {"customer": customer}


# ---- Administrator session ---------------------------------------------------------

@router.post("/api/admin/session")
def create_admin_session(payload: CustomerLogin, request: Request, response: Response) -> dict[str, str]:
    require_same_origin(request)
    if not admin_auth_services.configured():
        raise HTTPException(status_code=503, detail="Administrator credentials have not been configured.")
    # In production the admin login counter must survive restarts and serverless instances; local/test
    # runs keep the in-process limiter so a full automated suite is not throttled by earlier tests.
    store = otp_store if settings.production else limiter
    _throttle("admin-login-ip", _client_address(request), LOGIN_LIMIT, "Too many sign-in attempts. Try again in a few minutes.", store)
    _throttle("admin-login-email", payload.email, LOGIN_LIMIT, "Too many sign-in attempts for this account. Try again in a few minutes.", store)
    if not admin_auth_services.authenticate(payload.email, payload.password):
        raise HTTPException(status_code=401, detail="Administrator credentials are incorrect.")
    create_session_cookie(response, request, settings.admin_email, "admin")
    return {"mode": "authenticated"}


@router.post("/api/admin/password-reset/request")
def request_admin_password_reset(payload: PasswordResetRequest, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    if not settings.admin_email:
        admin_auth_services.log_missing_settings()
        raise HTTPException(status_code=503, detail="Administrator credentials have not been configured.")
    if not settings.smtp_configured:
        auth_services.log_missing_smtp_settings()
        raise HTTPException(status_code=503, detail="Password reset emails are not available right now. Contact the shop owner.")
    _throttle("admin-otp-request-ip", _client_address(request), OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", otp_store)
    # The admin address is checked before any code is created or emailed, so the form can say
    # plainly that the address does not belong to the administrator account.
    if not admin_auth_services.is_admin_email(payload.email):
        raise HTTPException(status_code=404, detail=admin_auth_services.NOT_ADMIN_EMAIL_MESSAGE)
    cooled_down = not otp_store.allow("admin-otp-cooldown", payload.email, 1, OTP_RESEND_COOLDOWN_SECONDS)
    _throttle("admin-otp-request-email", payload.email, OTP_REQUEST_LIMIT, "Too many verification requests. Try again later.", otp_store)
    if cooled_down:
        # No new code is sent; the code from the previous request stays valid until it expires.
        return {
            "message": "A code was requested for this address less than a minute ago. Use the code from that email, or wait to request a new one.",
            "resend_after": OTP_RESEND_COOLDOWN_SECONDS,
            "cooldown": True,
        }
    try:
        result = admin_auth_services.request_password_reset(payload.email)
    except HTTPException:
        # No email went out, so give the cooldown back: retry straight away.
        otp_store.reset("admin-otp-cooldown", payload.email)
        raise
    result["resend_after"] = OTP_RESEND_COOLDOWN_SECONDS
    return result


@router.post("/api/admin/password-reset/verify")
def verify_admin_password_reset(payload: PasswordResetVerify, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("admin-otp-verify-ip", _client_address(request), OTP_VERIFY_LIMIT, "Too many verification attempts. Try again later.", otp_store)
    _throttle("admin-otp-verify-email", payload.email, OTP_VERIFY_LIMIT, "Too many verification attempts. Try again later.", otp_store)
    token = admin_auth_services.verify_password_reset_code(payload.email, payload.code)
    return {"reset_token": token, "message": "Code verified. Choose your new password."}


@router.post("/api/admin/password-reset/confirm")
def confirm_admin_password_reset(payload: PasswordResetConfirm, request: Request) -> dict[str, str]:
    require_same_origin(request)
    _throttle("admin-reset-confirm-ip", _client_address(request), RESET_CONFIRM_LIMIT, "Too many attempts. Try again later.", otp_store)
    _throttle("admin-reset-confirm-email", payload.email, RESET_CONFIRM_LIMIT, "Too many attempts. Try again later.", otp_store)
    admin_auth_services.complete_password_reset(payload.email, payload.reset_token, payload.new_password)
    return {"message": "Your administrator password has been changed. Sign in with your new password."}


@router.get("/api/admin/session")
def admin_session(request: Request) -> dict[str, str]:
    claims = require_session(request, "admin")
    return {"mode": "authenticated", "email": claims["sub"]}


@router.delete("/api/admin/session", status_code=204)
def delete_admin_session(request: Request, response: Response) -> Response:
    require_same_origin(request)
    clear_session_cookie(response, request)
    response.status_code = 204
    return response
