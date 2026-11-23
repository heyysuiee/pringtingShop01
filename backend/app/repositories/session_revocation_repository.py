"""Server-side revocation for signed session tokens.

The session cookie is self-contained (HMAC-signed claims), so clearing it in the browser is not enough:
a copy of the token stays valid until its expiry. Logging out records an HMAC of the exact token in this
table; every authenticated request checks it. Only the logged-out token is revoked, so other devices
stay signed in. Rows are removed once their token would have expired anyway.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from backend.app.core.config import settings
from backend.app.database.connection import connection_scope, exclusive_write


def initialize() -> None:
    with exclusive_write() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS revoked_sessions (
                token_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_revoked_sessions_expiry ON revoked_sessions (expires_at)"
        )


def _token_hash(token: str) -> str:
    message = f"session-revocation:{token}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def revoke(token: str | None, expires_at: float) -> None:
    """Record one token as logged out. An empty token is a no-op."""
    if not token:
        return
    now = time.time()
    with connection_scope() as connection:
        connection.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            "INSERT OR REPLACE INTO revoked_sessions (token_hash, expires_at) VALUES (?, ?)",
            (_token_hash(token), expires_at),
        )


def is_revoked(token: str | None) -> bool:
    if not token:
        return False
    row = None
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT 1 FROM revoked_sessions WHERE token_hash = ? AND expires_at > ?",
            (_token_hash(token), time.time()),
        ).fetchone()
    return row is not None


def clear() -> None:
    """Remove every revocation (used by the automated tests)."""
    with connection_scope() as connection:
        connection.execute("DELETE FROM revoked_sessions")
