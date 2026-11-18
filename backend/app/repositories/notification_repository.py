"""Customer notifications (new or revised quotation, verified payment, confirmed order, ...).

Each notification has a ``dedupe_key`` with a UNIQUE index, and is inserted with INSERT OR IGNORE in the
same transaction as the order change that caused it. Repeating the same event (a retried request, a
double-clicked button that slipped past the version check) can therefore never notify the customer twice.
"""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from backend.app.database.connection import connection_scope
from backend.app.utils.clock import now_iso


def initialize() -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_notifications (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                customer_id TEXT,
                customer_email TEXT NOT NULL DEFAULT '',
                order_reference TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_notifications_dedupe ON customer_notifications(dedupe_key)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_notifications_customer ON customer_notifications(customer_id, created_at)"
        )


def insert(connection: sqlite3.Connection, reference: str, at: str, notification: dict[str, Any]) -> bool:
    """Insert a notification in the caller's transaction. Returns False when it was already sent."""
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO customer_notifications (
            id, dedupe_key, customer_id, customer_email, order_reference, kind, title, message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            secrets.token_hex(12),
            notification["dedupe_key"],
            notification.get("customer_id"),
            notification.get("customer_email", ""),
            reference,
            notification["kind"],
            notification["title"],
            notification["message"],
            at,
        ),
    )
    return cursor.rowcount == 1


def _view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "order_reference": row["order_reference"],
        "kind": row["kind"],
        "title": row["title"],
        "message": row["message"],
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
    }


def list_for_customer(customer_id: str, limit: int = 50) -> dict[str, Any]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM customer_notifications WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (customer_id, limit),
        ).fetchall()
        unread = connection.execute(
            "SELECT COUNT(*) AS total FROM customer_notifications WHERE customer_id = ? AND read_at IS NULL",
            (customer_id,),
        ).fetchone()
    return {"notifications": [_view(row) for row in rows], "unread": int(unread["total"]) if unread else 0}


def list_for_order(reference: str) -> list[dict[str, Any]]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM customer_notifications WHERE order_reference = ? ORDER BY created_at, rowid",
            (reference,),
        ).fetchall()
    return [_view(row) for row in rows]


def mark_read(customer_id: str, ids: list[str] | None = None) -> int:
    """Mark the customer's notifications (all, or only ``ids``) as read. Other customers' rows are untouched."""
    with connection_scope() as connection:
        if ids is None:
            cursor = connection.execute(
                "UPDATE customer_notifications SET read_at = ? WHERE customer_id = ? AND read_at IS NULL",
                (now_iso(), customer_id),
            )
        else:
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            cursor = connection.execute(
                f"UPDATE customer_notifications SET read_at = ? WHERE customer_id = ? AND read_at IS NULL AND id IN ({placeholders})",
                (now_iso(), customer_id, *ids),
            )
    return max(cursor.rowcount, 0)
