"""Append-only audit trail of important order, quotation, and payment changes.

Every entry is written in the same transaction as the order change it describes, so an order can never
change without its audit entry (and a refused change leaves no entry). Entries are never updated or deleted.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from backend.app.database.connection import connection_scope


def initialize() -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_audit_log (
                id TEXT PRIMARY KEY,
                order_reference TEXT NOT NULL,
                at TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_audit_reference ON order_audit_log(order_reference, at)"
        )


def insert(connection: sqlite3.Connection, reference: str, at: str, entry: dict[str, Any]) -> None:
    """Write one audit entry using the caller's open transaction."""
    connection.execute(
        """
        INSERT INTO order_audit_log (id, order_reference, at, actor_type, actor_id, action, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            secrets.token_hex(12),
            reference,
            at,
            str(entry.get("actor_type") or "system"),
            str(entry.get("actor_id") or ""),
            str(entry["action"]),
            json.dumps(entry.get("details") or {}),
        ),
    )


def list_for_order(reference: str) -> list[dict[str, Any]]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM order_audit_log WHERE order_reference = ? ORDER BY at, rowid",
            (reference,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "at": row["at"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "details": json.loads(row["details"] or "{}"),
        }
        for row in rows
    ]
