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
                pricing TEXT NOT NULL DEFAULT '{}',
                completed_at TEXT
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
        if "completed_at" not in columns:
            # When the administrator marked the order Completed. Used for completion-duration reports.
            connection.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)")
        _backfill_completed_at(connection)


def _backfill_completed_at(connection: sqlite3.Connection) -> None:
    """Fill completed_at for orders completed before the column existed.

    The completion time is read from the order's own history entry that recorded the move to
    Completed, so no date is invented. If no such entry exists the last history time is used.
    """
    rows = connection.execute(
        "SELECT reference, history, updated_at FROM orders WHERE status = 'Completed' AND completed_at IS NULL"
    ).fetchall()
    for row in rows:
        history = json.loads(row["history"] or "[]")
        completed = next(
            (entry["at"] for entry in reversed(history) if "to Completed" in str(entry.get("text", ""))),
            history[-1]["at"] if history else row["updated_at"],
        )
        connection.execute("UPDATE orders SET completed_at = ? WHERE reference = ?", (completed, row["reference"]))


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


class StaleOrderError(Exception):
    """Raised when an order changed after the caller read it (another action was saved first)."""


def order_version(order: OrderRecord) -> int:
    """A counter that increases with every saved change: each update appends exactly one history entry."""
    return len(order["history"])


def _apply_updates(order: OrderRecord, updates: OrderUpdates) -> None:
    """Copy the fields that may change after an order is created; nothing else can be overwritten.

    Each field is copied explicitly (rather than order.update(updates)) so a stray key such as
    "reference" or "customer" can never replace order data, and the type checker can verify it.
    """
    if "status" in updates:
        order["status"] = updates["status"]
    if "total_amount" in updates:
        order["total_amount"] = updates["total_amount"]
    if "payment_instructions" in updates:
        order["payment_instructions"] = updates["payment_instructions"]
    if "payments" in updates:
        order["payments"] = updates["payments"]
    if "attachments" in updates:
        order["attachments"] = updates["attachments"]
    if "draft_attachment" in updates:
        order["draft_attachment"] = updates["draft_attachment"]
    if "admin_notes" in updates:
        order["admin_notes"] = updates["admin_notes"]


def update_order(
    reference: str,
    updates: OrderUpdates,
    event: str,
    expected_version: int | None = None,
) -> OrderRecord | None:
    """Apply ``updates`` and append ``event`` to the history in one write transaction.

    When ``expected_version`` is given, the update is refused with StaleOrderError if the order was
    changed in the meantime, so a double-submitted or out-of-date action is never applied twice.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = decode_row(connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone())
        if current is None:
            return None
        if expected_version is not None and order_version(current) != expected_version:
            raise StaleOrderError(reference)
        previous_status = current["status"]
        _apply_updates(current, updates)
        current["updated_at"] = now_iso()
        current["history"].append({"at": current["updated_at"], "text": event})
        if current["status"] == "Completed" and previous_status != "Completed":
            current["completed_at"] = current["updated_at"]
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
            "completed_at": current.get("completed_at"),
        }
        connection.execute(
            """
            UPDATE orders SET updated_at = :updated_at, status = :status,
                total_amount = :total_amount, payment_instructions = :payment_instructions,
                payments = :payments, attachments = :attachments,
                draft_attachment = :draft_attachment, history = :history,
                admin_notes = :admin_notes, completed_at = :completed_at
            WHERE reference = :reference
            """,
            {**values, "reference": reference},
        )
    return current
