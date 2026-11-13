from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from backend.app.core.config import settings
from backend.app.repositories import admin_repository, customer_repository


SESSION_COOKIE = "impress_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
# "Remember me" on the customer sign-in page keeps the session for 30 days.
REMEMBER_TTL_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(expected_text + "=" * (-len(expected_text) % 4))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_session_token(
    subject: str,
    role: str,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    session_version: int | None = None,
) -> str:
    claims: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(12),
    }
    if session_version is not None:
        # Customer sessions record the account's session version; see session_claims().
        claims["ver"] = session_version
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(settings.secret_key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(settings.secret_key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("exp"), int):
        return None
    if claims["exp"] <= int(time.time()):
        return None
    if not isinstance(claims.get("sub"), str) or not isinstance(claims.get("role"), str):
        return None
    return claims


def secure_cookie(request: Request) -> bool:
    return settings.production or request.url.scheme == "https"


def create_session_cookie(
    response: Response,
    request: Request,
    subject: str,
    role: str,
    remember: bool | None = None,
) -> None:
    """Set the signed session cookie.

    ``remember=True`` keeps the customer signed in for 30 days. ``remember=False`` uses a browser
    session cookie (removed when the browser closes, and never valid for more than 12 hours).
    ``None`` keeps the standard 12-hour cookie (administrators and new registrations).
    """
    ttl = REMEMBER_TTL_SECONDS if remember else SESSION_TTL_SECONDS
    if role == "customer":
        version = customer_repository.get_session_version(subject)
    elif role == "admin":
        version = admin_repository.get_session_version()
    else:
        version = None
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(subject, role, ttl, version),
        max_age=None if remember is False else ttl,
        httponly=True,
        secure=secure_cookie(request),
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict", path="/")
    response.delete_cookie("impress_admin", httponly=True, samesite="strict", path="/api/admin")


def session_claims(request: Request, role: str | None = None) -> dict[str, Any] | None:
    claims = read_session_token(request.cookies.get(SESSION_COOKIE))
    if not claims or (role is not None and claims["role"] != role):
        return None
    if claims["role"] == "customer":
        # A password reset raises the account's session version, so every cookie issued before it
        # (on this or any other device) stops working. Cookies from before versioning count as 0.
        current = customer_repository.get_session_version(claims["sub"])
        if current is None or claims.get("ver", 0) != current:
            return None
    elif claims["role"] == "admin":
        # The administrator password reset also signs out sessions issued before the change.
        if claims.get("ver", 0) != admin_repository.get_session_version():
            return None
    return claims


def require_session(request: Request, role: str) -> dict[str, Any]:
    claims = session_claims(request, role)
    if claims is None:
        raise HTTPException(status_code=401, detail="A valid signed-in session is required.")
    return claims


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        parsed_origin = urlsplit(origin)
        origin_hostname = parsed_origin.hostname
        origin_port = parsed_origin.port
        origin_host = parsed_origin.netloc.casefold()
        origin_key = f"{parsed_origin.scheme.casefold()}://{origin_host}"
        valid_origin = (
            parsed_origin.scheme.casefold() in {"http", "https"}
            and bool(origin_hostname)
            and (origin_port is None or 1 <= origin_port <= 65535)
            and parsed_origin.path in ("", "/")
            and not parsed_origin.query
            and not parsed_origin.fragment
            and parsed_origin.username is None
            and parsed_origin.password is None
        )
        same_origin = valid_origin and (
            parsed_origin.scheme.casefold() == request.url.scheme.casefold()
            and origin_host == request.url.netloc.casefold()
        )
        configured_cross_origin = valid_origin and origin_key in {
            configured.rstrip("/").casefold() for configured in settings.cors_origins
        }
    except ValueError:
        valid_origin = False
        same_origin = False
        configured_cross_origin = False
    if not valid_origin or not (same_origin or configured_cross_origin):
        raise HTTPException(status_code=403, detail="Cross-origin requests are not allowed.")