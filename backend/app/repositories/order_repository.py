from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, cast

from backend.app.core.config import settings
from backend.app.repositories import customer_repository
from backend.app.database.connection import connection_scope
from backend.app.models.order import OrderRecord, OrderUpdates
from backend.app.utils.clock import now_iso
from backend.app.utils.order_number import base_reference, candidate_reference


UPLOAD_DIR = settings.upload_dir


def initialize() -> None:
    customer_repository.initialize()
    with connection_scope() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                reference TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                customer TEXT NOT NULL,
                product TEXT NOT NULL,
                customization TEXT NOT NULL,
                event_details TEXT NOT NULL,
                has_event INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL,
                status TEXT NOT NULL,
                total_amount REAL,
                payment_instructions TEXT NOT NULL DEFAULT '',
                payments TEXT NOT NULL DEFAULT '[]',
                attachments TEXT NOT NULL DEFAULT '[]',
                draft_attachment TEXT,
                history TEXT NOT NULL DEFAULT '[]',
                admin_notes TEXT NOT NULL DEFAULT '',
                customer_id TEXT REFERENCES customer_accounts(id) ON DELETE SET NULL,
                idempotency_key TEXT,
                pricing TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
        if "customer_id" not in columns:
            connection.execute(
                "ALTER TABLE orders ADD COLUMN customer_id TEXT REFERENCES customer_accounts(id) ON DELETE SET NULL"
            )
        if "idempotency_key" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
        if "pricing" not in columns:
            # Price estimate calculated from the administrator's catalog prices when the order was placed.
            connection.execute("ALTER TABLE orders ADD COLUMN pricing TEXT NOT NULL DEFAULT '{}'")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)"
        )


def decode_row(row: sqlite3.Row | None) -> OrderRecord | None:
    if row is None:
        return None
    result: dict[str, Any] = dict(row)
    for key in ("customer", "product", "customization", "event_details", "payments", "attachments", "history"):
        result[key] = json.loads(result[key])
    result["pricing"] = json.loads(result.get("pricing") or "{}")
    if result["draft_attachment"]:
        result["draft_attachment"] = json.loads(result["draft_attachment"])
    return cast(OrderRecord, result)


def get_order(reference: str) -> OrderRecord | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone()
    return decode_row(row)


def list_orders(email: str | None = None) -> list[OrderRecord]:
    with connection_scope() as connection:
        if email is None:
            rows = connection.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM orders WHERE lower(json_extract(customer, '$.email')) = ? ORDER BY created_at DESC",
                (email.lower(),),
            ).fetchall()
    return [decoded for row in rows if (decoded := decode_row(row)) is not None]


def list_orders_for_customer(customer_id: str) -> list[OrderRecord]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
    return [decoded for row in rows if (decoded := decode_row(row)) is not None]


def get_order_by_idempotency_key(key: str) -> OrderRecord | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    return decode_row(row)


def _reserve_reference(connection: sqlite3.Connection, created_at: str) -> str:
    """Pick the next free XXX-MM-DD-YY reference. Must run inside a BEGIN IMMEDIATE transaction."""
    base = base_reference(datetime.fromisoformat(created_at))
    sequence = 1
    while True:
        candidate = candidate_reference(base, sequence)
        taken = connection.execute("SELECT 1 FROM orders WHERE reference = ?", (candidate,)).fetchone()
        if taken is None:
            return candidate
        sequence += 1


def create_order(order: dict[str, Any], idempotency_key: str) -> str:
    """Insert an order and return its server-generated reference number.

    The reference is chosen inside an immediate write transaction, so concurrent submissions
    are serialized and cannot receive the same reference.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reference = _reserve_reference(connection, order["created_at"])
        connection.execute(
            """
            INSERT INTO orders (
                reference, created_at, updated_at, customer, product, customization,
                event_details, has_event, acknowledged, status, total_amount,
                attachments, history, customer_id, idempotency_key, pricing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference,
                order["created_at"],
                order["updated_at"],
                json.dumps(order["customer"]),
                json.dumps(order["product"]),
                json.dumps(order["customization"]),
                json.dumps(order["event_details"]),
                int(order["has_event"]),
                1,
                order["status"],
                None,
                json.dumps(order["attachments"]),
                json.dumps(order["history"]),
                order.get("customer_id"),
                idempotency_key,
                json.dumps(order.get("pricing") or {}),
            ),
        )
    order["reference"] = reference
    return reference


def update_order(reference: str, updates: OrderUpdates, event: str) -> OrderRecord | None:
    current = get_order(reference)
    if current is None:
        return None
    # OrderUpdates only contains columns that may change after an order is created.
    current.update(updates)
    current["updated_at"] = now_iso()
    current["history"].append({"at": current["updated_at"], "text": event})
    values = {
        "updated_at": current["updated_at"],
        "status": current["status"],
        "total_amount": current["total_amount"],
        "payment_instructions": current["payment_instructions"],
        "payments": json.dumps(current["payments"]),
        "attachments": json.dumps(current["attachments"]),
        "draft_attachment": json.dumps(current["draft_attachment"]) if current["draft_attachment"] else None,
        "history": json.dumps(current["history"]),
        "admin_notes": current["admin_notes"],
    }
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE orders SET updated_at = :updated_at, status = :status,
                total_amount = :total_amount, payment_instructions = :payment_instructions,
                payments = :payments, attachments = :attachments,
                draft_attachment = :draft_attachment, history = :history,
                admin_notes = :admin_notes
            WHERE reference = :reference
            """,
            {**values, "reference": reference},
        )
    return current
