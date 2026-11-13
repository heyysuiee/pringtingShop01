"""Storage for the single administrator account.

The administrator's email still comes from PRINTSHOP_ADMIN_EMAIL, but a password chosen through
"Forgot password?" is saved here and takes precedence over PRINTSHOP_ADMIN_PASSWORD_HASH. The
tables also hold the one active verification code / reset token, like customer_password_resets.
"""
from __future__ import annotations

from typing import Any

from backend.app.database.connection import connection_scope


ADMIN_ID = "admin"


def initialize() -> None:
    with connection_scope() as connection:
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
    """Save the new password hash, sign the administrator out everywhere, and remove the reset record.

    All three happen in one transaction, so the code and token are single use.
    """
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


def delete_reset() -> None:
    """Invalidate any OTP or reset token for the administrator."""
    with connection_scope() as connection:
        connection.execute("DELETE FROM admin_password_resets WHERE id = ?", (ADMIN_ID,))
