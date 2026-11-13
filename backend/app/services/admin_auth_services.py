"""Administrator sign in and "Forgot password?" (email OTP).

Only the address in PRINTSHOP_ADMIN_EMAIL can reset the administrator password. Codes and reset
tokens are stored hashed, are single use, and expire like the customer ones.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.core.security import hash_password, verify_password
from backend.app.repositories import admin_repository
from backend.app.services import email_service
from backend.app.utils.clock import now_iso, now_utc


OTP_LIFETIME = timedelta(minutes=5)
OTP_LIFETIME_SECONDS = int(OTP_LIFETIME.total_seconds())
RESET_TOKEN_LIFETIME = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
PASSWORD_MIN_LENGTH = 12
NOT_ADMIN_EMAIL_MESSAGE = "That email address is not the administrator account."


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _keyed_digest(purpose: str, value: str) -> str:
    message = f"{purpose}:{admin_repository.ADMIN_ID}:{value}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def is_admin_email(email: str) -> bool:
    """True only for the address in PRINTSHOP_ADMIN_EMAIL (case-insensitive)."""
    return bool(settings.admin_email) and email.strip().lower() == settings.admin_email


def configured() -> bool:
    """True when an administrator email and some password (env or saved by a reset) exist."""
    return bool(settings.admin_email) and bool(settings.admin_password_hash or admin_repository.get_password_hash())


def log_missing_settings() -> None:
    email_service.logger.error(
        "Administrator password reset requested but PRINTSHOP_ADMIN_EMAIL is not configured. "
        "Set it (and PRINTSHOP_SMTP_HOST / PRINTSHOP_SMTP_FROM for email) in the project-root .env file."
    )


def authenticate(email: str, password: str) -> bool:
    """Check the administrator credentials. A password saved by a reset overrides the env hash."""
    if not is_admin_email(email):
        return False
    encoded = admin_repository.get_password_hash() or settings.admin_password_hash
    return bool(encoded) and verify_password(password, encoded)


def request_password_reset(email: str) -> dict[str, Any]:
    """Send a one-time code to the administrator address; any other address is rejected."""
    if not is_admin_email(email):
        raise HTTPException(status_code=404, detail=NOT_ADMIN_EMAIL_MESSAGE)
    response = {"message": "A verification code was sent to the administrator email address.", "expires_in": OTP_LIFETIME_SECONDS}

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (now_utc() + OTP_LIFETIME).isoformat(timespec="seconds")
    admin_repository.replace_reset_code(_keyed_digest("otp", code), expires_at, now_iso())
    try:
        email_service.send_admin_password_reset_code(
            settings.admin_email,
            code,
            int(OTP_LIFETIME.total_seconds() // 60),
        )
    except email_service.EmailDeliveryError as error:
        admin_repository.delete_reset()
        raise HTTPException(status_code=503, detail=str(error)) from error
    return response


def verify_password_reset_code(email: str, code: str) -> str:
    """Check the OTP and return a short-lived reset token for the password step."""
    invalid = HTTPException(status_code=422, detail="The verification code is invalid or has expired.")
    if not is_admin_email(email):
        raise invalid
    record = admin_repository.get_reset()
    if record is None or not record["code_hash"]:
        raise invalid
    if _parse_time(record["code_expires_at"]) <= now_utc():
        admin_repository.delete_reset()
        raise invalid
    if record["attempts"] >= OTP_MAX_ATTEMPTS:
        admin_repository.delete_reset()
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")

    supplied = _keyed_digest("otp", code.strip())
    if not hmac.compare_digest(record["code_hash"], supplied):
        attempts = admin_repository.increment_reset_attempts()
        if attempts >= OTP_MAX_ATTEMPTS:
            admin_repository.delete_reset()
            raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new verification code.")
        raise invalid

    token = secrets.token_urlsafe(32)
    token_expires_at = (now_utc() + RESET_TOKEN_LIFETIME).isoformat(timespec="seconds")
    admin_repository.set_reset_token(_keyed_digest("reset", token), token_expires_at)
    return token


def complete_password_reset(email: str, reset_token: str, new_password: str) -> None:
    invalid = HTTPException(status_code=422, detail="This password reset session has expired. Start again.")
    if not is_admin_email(email):
        raise invalid
    if len(new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=422, detail=f"Use at least {PASSWORD_MIN_LENGTH} characters for the new password.")
    record = admin_repository.get_reset()
    if (
        record is None
        or not record["reset_token_hash"]
        or not record["reset_token_expires_at"]
        or _parse_time(record["reset_token_expires_at"]) <= now_utc()
    ):
        admin_repository.delete_reset()
        raise invalid
    if not hmac.compare_digest(record["reset_token_hash"], _keyed_digest("reset", reset_token)):
        raise invalid

    current = admin_repository.get_password_hash() or settings.admin_password_hash
    if current and verify_password(new_password, current):
        raise HTTPException(status_code=422, detail="Choose a new password that is different from your current one.")

    # Single use: the password is saved and the code and reset token are removed together.
    admin_repository.apply_password_reset(hash_password(new_password), now_iso())
