from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from backend.app.database.connection import connection_scope


CUSTOMER_COLUMNS = "id, email, name, phone, messenger, password_hash, created_at, updated_at"


def initialize() -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_accounts (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                messenger TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # One active password-reset verification record per customer. Password hashes never leave this module.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_password_resets (
                customer_id TEXT PRIMARY KEY REFERENCES customer_accounts(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                code_expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                reset_token_hash TEXT,
                reset_token_expires_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Sessions carry this number; raising it signs the customer out everywhere (used after a password reset).
        # Added in place so existing accounts keep their data; they start at 0.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(customer_accounts)")}
        if "session_version" not in columns:
            connection.execute("ALTER TABLE customer_accounts ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")


def get_session_version(customer_id: str) -> int | None:
    """The account's current session version, or None if the account no longer exists."""
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT session_version FROM customer_accounts WHERE id = ?",
            (customer_id,),
        ).fetchone()
    return int(row["session_version"]) if row else None


def get_by_email(email: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute(
            f"SELECT {CUSTOMER_COLUMNS} FROM customer_accounts WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def get_by_id(customer_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute(
            f"SELECT {CUSTOMER_COLUMNS} FROM customer_accounts WHERE id = ?",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def create(customer: dict[str, str | None], now: str) -> dict[str, Any]:
    """Insert a customer account."""
    customer_id = str(uuid.uuid4())
    try:
        with connection_scope() as connection:
            connection.execute(
                """
                INSERT INTO customer_accounts
                    (id, email, name, phone, messenger, password_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_id,
                    str(customer["email"]).strip().lower(),
                    str(customer["name"]).strip(),
                    str(customer.get("phone") or "").strip(),
                    str(customer.get("messenger") or "").strip(),
                    customer["password_hash"],
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as error:
        raise ValueError("An account already exists for that email address.") from error
    created = get_by_id(customer_id)
    if created is None:
        raise RuntimeError("The registered customer could not be loaded.")
    return created


def update(customer_id: str, profile: dict[str, str], now: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        cursor = connection.execute(
            """
            UPDATE customer_accounts
            SET name = ?, phone = ?, messenger = ?, updated_at = ?
            WHERE id = ?
            """,
            (profile["name"].strip(), profile["phone"].strip(), profile.get("messenger", "").strip(), now, customer_id),
        )
        if cursor.rowcount != 1:
            return None
    return get_by_id(customer_id)


def set_password_hash(customer_id: str, password_hash: str, now: str) -> None:
    """Change the password and end every existing session for the account."""
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE customer_accounts
            SET password_hash = ?, updated_at = ?, session_version = session_version + 1
            WHERE id = ?
            """,
            (password_hash, now, customer_id),
        )


def replace_reset_code(customer_id: str, code_hash: str, code_expires_at: str, now: str) -> None:
    """Store a new OTP, discarding any previous code, attempts, and reset token."""
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO customer_password_resets
                (customer_id, code_hash, code_expires_at, attempts, reset_token_hash, reset_token_expires_at, created_at)
            VALUES (?, ?, ?, 0, NULL, NULL, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                code_hash = excluded.code_hash,
                code_expires_at = excluded.code_expires_at,
                attempts = 0,
                reset_token_hash = NULL,
                reset_token_expires_at = NULL,
                created_at = excluded.created_at
            """,
            (customer_id, code_hash, code_expires_at, now),
        )


def get_reset(customer_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT * FROM customer_password_resets WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def increment_reset_attempts(customer_id: str) -> int:
    with connection_scope() as connection:
        connection.execute(
            "UPDATE customer_password_resets SET attempts = attempts + 1 WHERE customer_id = ?",
            (customer_id,),
        )
        row = connection.execute(
            "SELECT attempts FROM customer_password_resets WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
    return int(row["attempts"]) if row else 0


def set_reset_token(customer_id: str, token_hash: str, token_expires_at: str) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE customer_password_resets
            SET reset_token_hash = ?, reset_token_expires_at = ?, code_hash = '', code_expires_at = ?
            WHERE customer_id = ?
            """,
            (token_hash, token_expires_at, token_expires_at, customer_id),
        )


def apply_password_reset(customer_id: str, password_hash: str, now: str) -> None:
    """Save the new password hash, sign the account out everywhere, and remove the reset record.

    All three happen in one transaction, so the code and token are single use and no old session
    (on any device) survives the password change.
    """
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE customer_accounts
            SET password_hash = ?, updated_at = ?, session_version = session_version + 1
            WHERE id = ?
            """,
            (password_hash, now, customer_id),
        )
        connection.execute("DELETE FROM customer_password_resets WHERE customer_id = ?", (customer_id,))


def delete_reset(customer_id: str) -> None:
    """Invalidate any OTP or reset token for the customer."""
    with connection_scope() as connection:
        connection.execute("DELETE FROM customer_password_resets WHERE customer_id = ?", (customer_id,))


def list_customers() -> list[dict[str, Any]]:
    with connection_scope() as connection:
        rows = connection.execute(
            """
            SELECT id, email, name, phone, messenger, created_at, updated_at
            FROM customer_accounts ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
