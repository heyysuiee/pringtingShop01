"""Storage for the single administrator account.

The administrator's email still comes from PRINTSHOP_ADMIN_EMAIL, but a password chosen through
"Forgot password?" is saved here and takes precedence over PRINTSHOP_ADMIN_PASSWORD_HASH. The
tables also hold the one active verification code / reset token, like customer_password_resets.
"""
from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Callable

from backend.app.database.connection import connection_scope, exclusive_write


ADMIN_ID = "admin"


def initialize() -> None:
    with exclusive_write() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_credentials (
                id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                session_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        # One active password-reset verification record for the administrator.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_password_resets (
                id TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                code_expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                reset_token_hash TEXT,
                reset_token_expires_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


# Administrator state kept in the shared app_meta key/value table (no extra tables): the notification
# read state and the backup/restore history. Keys use the "admin." and "backup." prefixes, which a
# database restore leaves unchanged.
SETTING_PREFIXES = ("admin.", "backup.")


def _ensure_meta(connection: Any) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def get_setting(key: str) -> str | None:
    if not key.startswith(SETTING_PREFIXES):
        raise ValueError("Administrator settings use the admin. or backup. prefix.")
    with connection_scope() as connection:
        _ensure_meta(connection)
        row = connection.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    if not key.startswith(SETTING_PREFIXES):
        raise ValueError("Administrator settings use the admin. or backup. prefix.")
    with connection_scope() as connection:
        _ensure_meta(connection)
        connection.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_password_hash() -> str | None:
    """The password hash saved by a reset, or None when the initial env hash is still in use."""
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT password_hash FROM admin_credentials WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
    return row["password_hash"] if row else None


def get_session_version() -> int:
    """The administrator's current session version (0 until a password reset raises it)."""
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT session_version FROM admin_credentials WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
    return int(row["session_version"]) if row else 0


def replace_reset_code(code_hash: str, code_expires_at: str, now: str) -> None:
    """Store a new OTP, discarding any previous code, attempts, and reset token."""
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO admin_password_resets
                (id, code_hash, code_expires_at, attempts, reset_token_hash, reset_token_expires_at, created_at)
            VALUES (?, ?, ?, 0, NULL, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                code_hash = excluded.code_hash,
                code_expires_at = excluded.code_expires_at,
                attempts = 0,
                reset_token_hash = NULL,
                reset_token_expires_at = NULL,
                created_at = excluded.created_at
            """,
            (ADMIN_ID, code_hash, code_expires_at, now),
        )


def get_reset() -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT * FROM admin_password_resets WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
    return dict(row) if row else None


def increment_reset_attempts() -> int:
    with connection_scope() as connection:
        connection.execute(
            "UPDATE admin_password_resets SET attempts = attempts + 1 WHERE id = ?",
            (ADMIN_ID,),
        )
        row = connection.execute(
            "SELECT attempts FROM admin_password_resets WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
    return int(row["attempts"]) if row else 0


def verify_reset_code(
    supplied_code_hash: str,
    max_attempts: int,
    token_hash: str,
    token_expires_at: str,
    now: datetime,
) -> dict[str, Any]:
    """Atomically check the administrator OTP and install a reset token (see customer_repository).

    Returns {"result": "ok" | "invalid" | "invalid_locked" | "expired" | "locked" | "missing",
             "attempts": int}.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT code_hash, code_expires_at, attempts FROM admin_password_resets WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
        if row is None or not row["code_hash"]:
            return {"result": "missing", "attempts": 0}
        attempts = int(row["attempts"] or 0)
        if datetime.fromisoformat(row["code_expires_at"]) <= now:
            connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
            return {"result": "expired", "attempts": attempts}
        if attempts >= max_attempts:
            connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
            return {"result": "locked", "attempts": attempts}
        if not hmac.compare_digest(str(row["code_hash"]), supplied_code_hash):
            attempts += 1
            if attempts >= max_attempts:
                connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
                return {"result": "invalid_locked", "attempts": attempts}
            connection.execute(
                "UPDATE admin_password_resets SET attempts = ? WHERE id = ?",
                (attempts, ADMIN_ID),
            )
            return {"result": "invalid", "attempts": attempts}
        connection.execute(
            """
            UPDATE admin_password_resets
            SET attempts = 0, code_hash = '', code_expires_at = ?,
                reset_token_hash = ?, reset_token_expires_at = ?
            WHERE id = ?
            """,
            (token_expires_at, token_hash, token_expires_at, ADMIN_ID),
        )
        return {"result": "ok", "attempts": 0}


def set_reset_token(token_hash: str, token_expires_at: str) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE admin_password_resets
            SET reset_token_hash = ?, reset_token_expires_at = ?, code_hash = '', code_expires_at = ?
            WHERE id = ?
            """,
            (token_hash, token_expires_at, token_expires_at, ADMIN_ID),
        )


def apply_password_reset(password_hash: str, now: str) -> None:
    """Save the new password hash, sign the administrator out everywhere, and remove the reset record."""
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO admin_credentials (id, password_hash, session_version, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                password_hash = excluded.password_hash,
                session_version = admin_credentials.session_version + 1,
                updated_at = excluded.updated_at
            """,
            (ADMIN_ID, password_hash, now),
        )
        connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))


def consume_password_reset(
    supplied_token_hash: str,
    password_hash: str,
    now: str,
    fallback_hash: str,
    is_same_password: Callable[[str], bool],
) -> str:
    """Validate the admin reset token and apply the password under one write lock.

    Token validation, the current-password check, the credential update, and token deletion are one
    transaction, so concurrent confirmations cannot both consume the same token. Returns ``"ok"``,
    ``"invalid"``, or ``"same"``.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reset = connection.execute(
            "SELECT reset_token_hash, reset_token_expires_at FROM admin_password_resets WHERE id = ?",
            (ADMIN_ID,),
        ).fetchone()
        if (
            reset is None
            or not reset["reset_token_hash"]
            or not reset["reset_token_expires_at"]
            or datetime.fromisoformat(reset["reset_token_expires_at"]) <= datetime.fromisoformat(now)
        ):
            connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
            return "invalid"
        if not hmac.compare_digest(str(reset["reset_token_hash"]), supplied_token_hash):
            return "invalid"
        credentials = connection.execute("SELECT password_hash FROM admin_credentials WHERE id = ?", (ADMIN_ID,)).fetchone()
        current = credentials["password_hash"] if credentials and credentials["password_hash"] else fallback_hash
        if is_same_password(current):
            return "same"
        connection.execute(
            """
            INSERT INTO admin_credentials (id, password_hash, session_version, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(id) DO UPDATE SET
                password_hash = excluded.password_hash,
                session_version = admin_credentials.session_version + 1,
                updated_at = excluded.updated_at
            """,
            (ADMIN_ID, password_hash, now),
        )
        connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
        return "ok"


def delete_reset() -> None:
    """Invalidate any OTP or reset token for the administrator."""
    with connection_scope() as connection:
        connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
