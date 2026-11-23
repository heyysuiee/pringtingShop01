from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.core.security import hash_password, verify_password
from backend.app.repositories import customer_repository
from backend.app.schemas.user import CustomerRegistration, password_problems, validate_phone
from backend.app.services import email_service, location_services
from backend.app.utils.clock import now_iso, now_utc


# Codes are valid for 5 minutes; the verified reset session then has 10 minutes to choose a password.
OTP_LIFETIME = timedelta(minutes=5)
OTP_LIFETIME_SECONDS = int(OTP_LIFETIME.total_seconds())
RESET_TOKEN_LIFETIME = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
GENERIC_REQUEST_MESSAGE = (
    "If an account exists for that email address, a verification code has been sent. "
    "Check your inbox and spam folder."
)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def public_customer(customer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": customer["id"],
        "email": customer["email"],
        "name": customer["name"],
        "phone": customer["phone"],
        "messenger": customer["messenger"],
        "created_at": customer["created_at"],
        "updated_at": customer["updated_at"],
        "email_verified": bool(customer.get("email_verified", 1)),
        # A reusable delivery address, validated against the same PSGC data as checkout.
        "saved_address": customer.get("saved_address") if isinstance(customer.get("saved_address"), dict) else {},
    }


def _issue_email_verification(customer: dict[str, Any]) -> dict[str, Any]:
    """Send a code to the address just registered and store only its keyed hash."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (now_utc() + OTP_LIFETIME).isoformat(timespec="seconds")
    customer_repository.replace_email_verification(
        customer["id"],
        _keyed_digest("email-verify", customer["id"], code),
        expires_at,
        now_iso(),
    )
    try:
        email_service.send_email_verification_code(customer["email"], customer["name"], code, int(OTP_LIFETIME.total_seconds() // 60))
    except email_service.EmailDeliveryError as error:
        customer_repository.delete_email_verification(customer["id"])
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"required": True, "expires_in": OTP_LIFETIME_SECONDS}


def register_customer(payload: CustomerRegistration, verify_email: bool | None = None) -> dict[str, Any]:
    customer = payload.model_dump()
    customer["password_hash"] = hash_password(customer.pop("password"))
    # Raises ValueError when the email address is already registered.
    created = customer_repository.create(customer, now_iso())
    public = public_customer(created)
    needs_verification = settings.require_email_verification if verify_email is None else verify_email
    if needs_verification:
        try:
            public["email_verification"] = _issue_email_verification(created)
        except HTTPException:
            # A code the customer can never receive must not reserve their email address.
            customer_repository.delete(created["id"])
            raise
    return public


def authenticate_customer(email: str, password: str) -> dict[str, Any] | None:
    customer = customer_repository.get_by_email(email)
    if customer is None or not verify_password(password, customer["password_hash"]):
        return None
    return public_customer(customer)


def get_customer(customer_id: str) -> dict[str, Any] | None:
    customer = customer_repository.get_by_id(customer_id)
    return public_customer(customer) if customer else None


def update_customer(customer_id: str, profile: dict[str, str]) -> dict[str, Any] | None:
    customer = customer_repository.update(customer_id, profile, now_iso())
    return public_customer(customer) if customer else None


def _saved_address_text(value: Any, label: str, limit: int, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise HTTPException(status_code=422, detail=f"{label} is required.")
    if len(text) > limit:
        raise HTTPException(status_code=422, detail=f"{label} must be {limit} characters or fewer.")
    return text


def save_delivery_address(customer_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and save the customer's reusable delivery address.

    The same PSGC province/city check and phone rule used at checkout run here. The saved address is a
    convenience for prefill only: the order's fee and region are always recalculated and snapshotted when
    the customer actually places the order.
    """
    fields = (
        ("recipient_name", "Recipient name", 120, True),
        ("contact_number", "Contact number", 40, True),
        ("street", "House number / Street", 200, True),
        ("barangay", "Barangay", 120, True),
        ("city", "City / Municipality", 120, True),
        ("province", "Province", 120, True),
        ("landmark", "Landmark", 200, False),
        ("delivery_instructions", "Delivery instructions", 500, False),
    )
    address = {
        key: _saved_address_text(payload.get(key), label, limit, required)
        for key, label, limit, required in fields
    }
    try:
        address["contact_number"] = validate_phone(address["contact_number"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"Delivery contact number: {error}") from error
    resolved = location_services.resolve_address(address["province"], address["city"])
    address.update({key: resolved[key] for key in ("province", "city", "region", "region_code", "island_group")})
    customer = customer_repository.update_saved_address(customer_id, address, now_iso())
    return public_customer(customer) if customer else None


def list_customers() -> list[dict[str, Any]]:
    """Every customer account for the admin, newest first. Password hashes are never included."""
    return customer_repository.list_customers()


# ---- Forgot password: email OTP -------------------------------------------------

def _keyed_digest(purpose: str, customer_id: str, value: str) -> str:
    message = f"{purpose}:{customer_id}:{value}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def log_missing_smtp_settings() -> None:
    email_service.logger.error(
        "Password reset requested but SMTP is not configured. Set PRINTSHOP_SMTP_HOST and PRINTSHOP_SMTP_FROM "
        "(plus PRINTSHOP_SMTP_USERNAME / PRINTSHOP_SMTP_PASSWORD) in the project-root .env file."
    )


EMAIL_VERIFICATION_MESSAGE = (
    "If an account exists for that email address and still needs verification, a code has been sent. "
    "Check your inbox and spam folder."
)


def request_email_verification(email: str) -> dict[str, Any]:
    """Resend the registration code. The response is the same for every address (no enumeration)."""
    response = {"message": EMAIL_VERIFICATION_MESSAGE, "expires_in": OTP_LIFETIME_SECONDS}
    customer = customer_repository.get_by_email(email)
    if customer is None or customer.get("email_verified"):
        return response
    _issue_email_verification(customer)
    return response


def verify_email_address(email: str, code: str) -> dict[str, Any]:
    """Check the registration code and, on success, allow the account to sign in."""
    invalid = HTTPException(status_code=422, detail="The verification code is invalid or has expired.")
    customer = customer_repository.get_by_email(email)
    if customer is None or customer.get("email_verified"):
        raise invalid
    result = customer_repository.verify_email_code(
        customer["id"],
        _keyed_digest("email-verify", customer["id"], code.strip()),
        OTP_MAX_ATTEMPTS,
        now_iso(),
        now_utc(),
    )
    if result["result"] in {"locked", "invalid_locked"}:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")
    if result["result"] != "ok":
        raise invalid
    return {"message": "Email address verified. You can sign in now.", "customer": public_customer(customer_repository.get_by_id(customer["id"]) or customer)}


def request_password_reset(email: str) -> dict[str, Any]:
    """Send a one-time code if the account can receive one. The response never reveals whether it exists."""
    # The same response is returned for unknown addresses, so it cannot reveal whether an account exists.
    response = {"message": GENERIC_REQUEST_MESSAGE, "expires_in": OTP_LIFETIME_SECONDS}
    customer = customer_repository.get_by_email(email)
    if customer is None:
        return response

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (now_utc() + OTP_LIFETIME).isoformat(timespec="seconds")
    customer_repository.replace_reset_code(
        customer["id"],
        _keyed_digest("otp", customer["id"], code),
        expires_at,
        now_iso(),
    )
    try:
        email_service.send_password_reset_code(
            customer["email"],
            customer["name"],
            code,
            int(OTP_LIFETIME.total_seconds() // 60),
        )
    except email_service.EmailDeliveryError as error:
        customer_repository.delete_reset(customer["id"])
        raise HTTPException(status_code=503, detail=str(error)) from error
    return response


def verify_password_reset_code(email: str, code: str) -> str:
    """Check the OTP and return a short-lived reset token for the password step.

    The comparison, attempt counting and token issue are one atomic transaction in the repository, so
    parallel guesses cannot slip past the five-attempt limit or cancel a code issued at the same time.
    """
    invalid = HTTPException(status_code=422, detail="The verification code is invalid or has expired.")
    customer = customer_repository.get_by_email(email)
    if customer is None:
        raise invalid
    token = secrets.token_urlsafe(32)
    now = now_utc()
    result = customer_repository.verify_reset_code(
        customer["id"],
        _keyed_digest("otp", customer["id"], code.strip()),
        OTP_MAX_ATTEMPTS,
        _keyed_digest("reset", customer["id"], token),
        (now + RESET_TOKEN_LIFETIME).isoformat(timespec="seconds"),
        now,
    )
    if result["result"] in {"locked", "invalid_locked"}:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")
    if result["result"] != "ok":
        raise invalid
    return token


def complete_password_reset(email: str, reset_token: str, new_password: str) -> None:
    invalid = HTTPException(status_code=422, detail="This password reset session has expired. Start again.")
    problems = password_problems(new_password)
    if problems:
        raise HTTPException(status_code=422, detail="The new password does not meet the requirements: " + " ".join(problems))
    customer = customer_repository.get_by_email(email)
    if customer is None:
        raise invalid
    # The token check, password check, and consumption are one transaction: a token can be used once.
    result = customer_repository.consume_password_reset(
        customer["id"],
        _keyed_digest("reset", customer["id"], reset_token),
        hash_password(new_password),
        now_iso(),
        lambda current_hash: verify_password(new_password, current_hash),
    )
    if result == "invalid":
        raise invalid
    if result == "same":
        raise HTTPException(status_code=422, detail="Choose a new password that is different from your current one.")
