"""Rate limits for the forgot-password (OTP) endpoints, stored in the database.

Unlike the in-memory limiter in core/rate_limit.py, these limits survive a server restart:
the 60-second resend cooldown and the per-email / per-address request limits keep working
after the app is restarted. (Each code's expiry time and wrong-attempt count are stored in
customer_password_resets, see customer_repository.)

Each event is one row: (bucket, key_hash, created_at). The key (an email address or client
address) is stored only as an HMAC, so the table never holds plain email addresses.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from backend.app.core.config import settings
from backend.app.database.connection import connection_scope


# Rows older than this are deleted whatever their bucket (longer than any window used).
RETENTION_SECONDS = 24 * 60 * 60


def initialize() -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_throttle_events (
                bucket TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_throttle_lookup ON auth_throttle_events (bucket, key_hash, created_at)"
        )


def _key_hash(bucket: str, key: str) -> str:
    message = f"{bucket}:{key.strip().lower()}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def allow(bucket: str, key: str, limit: int, window_seconds: int) -> bool:
    """Record one event and return True, or return False if `limit` events already happened in the window.

    Runs in one IMMEDIATE transaction, so two simultaneous requests cannot both slip under the limit.
    """
    now = time.time()  # wall-clock time, so stored events still mean the same thing after a restart
    key_hash = _key_hash(bucket, key)
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM auth_throttle_events WHERE created_at <= ?", (now - RETENTION_SECONDS,))
        count = connection.execute(
            "SELECT COUNT(*) FROM auth_throttle_events WHERE bucket = ? AND key_hash = ? AND created_at > ?",
            (bucket, key_hash, now - window_seconds),
        ).fetchone()[0]
        if count >= limit:
            return False
        connection.execute(
            "INSERT INTO auth_throttle_events (bucket, key_hash, created_at) VALUES (?, ?, ?)",
            (bucket, key_hash, now),
        )
    return True


def reset(bucket: str, key: str) -> None:
    """Forget the events for one key (for example, give back the cooldown after a failed send)."""
    with connection_scope() as connection:
        connection.execute(
            "DELETE FROM auth_throttle_events WHERE bucket = ? AND key_hash = ?",
            (bucket, _key_hash(bucket, key)),
        )


def clear() -> None:
    """Delete every recorded event (used by the automated tests)."""
    with connection_scope() as connection:
        connection.execute("DELETE FROM auth_throttle_events")
