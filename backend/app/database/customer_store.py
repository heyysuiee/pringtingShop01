from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from backend.app.database.connection import connection_scope


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


def get_by_email(email: str) -> dict[str, Any] | None:
    initialize()
    with connection_scope() as connection:
        row = connection.execute(
            """
            SELECT id, email, name, phone, messenger, password_hash, created_at, updated_at
            FROM customer_accounts WHERE email = ?
            """,
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def get_by_id(customer_id: str) -> dict[str, Any] | None:
    initialize()
    with connection_scope() as connection:
        row = connection.execute(
            """
            SELECT id, email, name, phone, messenger, created_at, updated_at
            FROM customer_accounts WHERE id = ?
            """,
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def create(customer: dict[str, str], now: str) -> dict[str, Any]:
    initialize()
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
                    customer["email"].strip().lower(),
                    customer["name"].strip(),
                    customer["phone"].strip(),
                    customer.get("messenger", "").strip(),
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
    initialize()
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
        row = connection.execute(
            """
            SELECT id, email, name, phone, messenger, created_at, updated_at
            FROM customer_accounts WHERE id = ?
            """,
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def list_customers() -> list[dict[str, Any]]:
    initialize()
    with connection_scope() as connection:
        rows = connection.execute(
            """
            SELECT id, email, name, phone, messenger, created_at, updated_at
            FROM customer_accounts ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
