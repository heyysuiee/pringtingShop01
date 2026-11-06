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
from backend.app.schemas.user import CustomerRegistration
from backend.app.services import email_service
from backend.app.utils.clock import now_iso, now_utc


OTP_LIFETIME = timedelta(minutes=10)
RESET_TOKEN_LIFETIME = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
PASSWORD_MIN_LENGTH = 12
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
    }


def register_customer(payload: CustomerRegistration) -> dict[str, Any]:
    customer = payload.model_dump()
    customer["password_hash"] = hash_password(customer.pop("password"))
    # Raises ValueError when the email address is already registered.
    return public_customer(customer_repository.create(customer, now_iso()))


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


def list_customers() -> list[dict[str, Any]]:
    """Every customer account for the admin, newest first. Password hashes are never included."""
    return customer_repository.list_customers()


# ---- Forgot password: email OTP -------------------------------------------------

def _keyed_digest(purpose: str, customer_id: str, value: str) -> str:
    message = f"{purpose}:{customer_id}:{value}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def request_password_reset(email: str) -> dict[str, Any]:
    """Send a one-time code if the account can receive one. The response never reveals whether it exists."""
    customer = customer_repository.get_by_email(email)
    if customer is None:
        return {"message": GENERIC_REQUEST_MESSAGE}

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
    return {"message": GENERIC_REQUEST_MESSAGE}


def verify_password_reset_code(email: str, code: str) -> str:
    """Check the OTP and return a short-lived reset token for the password step."""
    invalid = HTTPException(status_code=422, detail="The verification code is invalid or has expired.")
    customer = customer_repository.get_by_email(email)
    if customer is None:
        raise invalid
    record = customer_repository.get_reset(customer["id"])
    if record is None or not record["code_hash"] or _parse_time(record["code_expires_at"]) <= now_utc():
        customer_repository.delete_reset(customer["id"])
        raise invalid
    if record["attempts"] >= OTP_MAX_ATTEMPTS:
        customer_repository.delete_reset(customer["id"])
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")

    expected = record["code_hash"]
    supplied = _keyed_digest("otp", customer["id"], code.strip())
    if not hmac.compare_digest(expected, supplied):
        attempts = customer_repository.increment_reset_attempts(customer["id"])
        if attempts >= OTP_MAX_ATTEMPTS:
            customer_repository.delete_reset(customer["id"])
            raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")
        raise invalid

    token = secrets.token_urlsafe(32)
    token_expires_at = (now_utc() + RESET_TOKEN_LIFETIME).isoformat(timespec="seconds")
    customer_repository.set_reset_token(customer["id"], _keyed_digest("reset", customer["id"], token), token_expires_at)
    return token


def complete_password_reset(email: str, reset_token: str, new_password: str) -> None:
    invalid = HTTPException(status_code=422, detail="This password reset session has expired. Start again.")
    if len(new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=422, detail=f"Use at least {PASSWORD_MIN_LENGTH} characters for the new password.")
    customer = customer_repository.get_by_email(email)
    if customer is None:
        raise invalid
    record = customer_repository.get_reset(customer["id"])
    if (
        record is None
        or not record["reset_token_hash"]
        or not record["reset_token_expires_at"]
        or _parse_time(record["reset_token_expires_at"]) <= now_utc()
    ):
        customer_repository.delete_reset(customer["id"])
        raise invalid
    supplied = _keyed_digest("reset", customer["id"], reset_token)
    if not hmac.compare_digest(record["reset_token_hash"], supplied):
        raise invalid

    customer_repository.set_password_hash(customer["id"], hash_password(new_password), now_iso())
    # Single use: removing the record invalidates the OTP and the reset token together.
    customer_repository.delete_reset(customer["id"])
