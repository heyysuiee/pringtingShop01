from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from backend.app.code.config import settings
from backend.app.database import customer_store
from backend.app.database.connection import connection_scope
from backend.app.models.order import OrderRecord, OrderUpdates


UPLOAD_DIR = settings.upload_dir


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialize() -> None:
    customer_store.initialize()
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
                idempotency_key TEXT
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
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)"
        )


def decode_row(row: sqlite3.Row | None) -> OrderRecord | None:
    if row is None:
        return None
    result: dict[str, Any] = dict(row)
    for key in ("customer", "product", "customization", "event_details", "payments", "attachments", "history"):
        result[key] = json.loads(result[key])
    if result["draft_attachment"]:
        result["draft_attachment"] = json.loads(result["draft_attachment"])
    return cast(OrderRecord, result)


def get_order(reference: str) -> OrderRecord | None:
    initialize()
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone()
    return decode_row(row)


def list_orders(email: str | None = None) -> list[OrderRecord]:
    initialize()
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
    initialize()
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
    return [decoded for row in rows if (decoded := decode_row(row)) is not None]


def get_order_by_idempotency_key(key: str) -> OrderRecord | None:
    initialize()
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    return decode_row(row)


def create_order(order: Mapping[str, Any], idempotency_key: str) -> None:
    initialize()
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO orders (
                reference, created_at, updated_at, customer, product, customization,
                event_details, has_event, acknowledged, status, total_amount,
                attachments, history, customer_id, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["reference"],
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
            ),
        )


def update_order(reference: str, updates: OrderUpdates, event: str) -> OrderRecord | None:
    initialize()
    current = get_order(reference)
    if current is None:
        return None
    if "status" in updates:
        current["status"] = updates["status"]
    if "total_amount" in updates:
        current["total_amount"] = updates["total_amount"]
    if "payment_instructions" in updates:
        current["payment_instructions"] = updates["payment_instructions"]
    if "payments" in updates:
        current["payments"] = updates["payments"]
    if "attachments" in updates:
        current["attachments"] = updates["attachments"]
    if "draft_attachment" in updates:
        current["draft_attachment"] = updates["draft_attachment"]
    if "admin_notes" in updates:
        current["admin_notes"] = updates["admin_notes"]
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
