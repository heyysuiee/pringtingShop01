from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, cast

from backend.app.core.config import settings
from backend.app.repositories import audit_repository, customer_repository, notification_repository, reference_claim_repository
from backend.app.database.connection import connection_scope, exclusive_write
from backend.app.models.order import OrderRecord, OrderUpdates
from backend.app.utils.clock import now_iso
from backend.app.utils.order_number import base_reference, candidate_reference


UPLOAD_DIR = settings.upload_dir
# JSON columns added for production monitoring, draft history, cancellations, and refunds, with their empty values.
# work_approval also holds the finished-work preview, the customer's approval timestamp, and revision requests.
NEW_JSON_COLUMNS = {"production": "{}", "drafts": "[]", "cancellation_requests": "[]", "refunds": "[]", "work_approval": "{}"}


def initialize() -> None:
    customer_repository.initialize()
    audit_repository.initialize()
    notification_repository.initialize()
    with exclusive_write() as connection:
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
        if "required_down_payment" not in columns:
            # Administrator-set down payment. NULL means the original 50% rule is used (older orders).
            connection.execute("ALTER TABLE orders ADD COLUMN required_down_payment REAL")
        if "payment_status" not in columns:
            # Payment progress is tracked separately from the order/work status.
            connection.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'Unpaid'")
        if "payment_option" not in columns:
            # How the customer pays: "down_payment" (50% after the quotation, balance before release)
            # or "cod" (cash on delivery: no down payment, the full amount is paid on delivery/pickup).
            connection.execute("ALTER TABLE orders ADD COLUMN payment_option TEXT NOT NULL DEFAULT 'down_payment'")
        if "fulfillment" not in columns:
            # Delivery or store pickup (JSON). Existing orders keep '{}', which means store pickup.
            connection.execute("ALTER TABLE orders ADD COLUMN fulfillment TEXT NOT NULL DEFAULT '{}'")
        if "quotation" not in columns:
            # The administrator's quotation, COD permission, and the customer's acceptance (JSON).
            connection.execute("ALTER TABLE orders ADD COLUMN quotation TEXT NOT NULL DEFAULT '{}'")
        if "payment_submissions" not in columns:
            # Payments reported by the customer with a reference/proof, waiting for verification (JSON).
            connection.execute("ALTER TABLE orders ADD COLUMN payment_submissions TEXT NOT NULL DEFAULT '[]'")
        if "quotation_history" not in columns:
            # Every quotation the administrator sent (amount, previous amount, note, timestamps, response) (JSON).
            connection.execute("ALTER TABLE orders ADD COLUMN quotation_history TEXT NOT NULL DEFAULT '[]'")
        for column, default in NEW_JSON_COLUMNS.items():
            if column not in columns:
                # Additive migration: existing orders get the empty default and keep every other value.
                connection.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key ON orders(idempotency_key)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)")
        reference_claim_repository.initialize_with(connection)
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
    result["required_down_payment"] = result.get("required_down_payment")
    result["payment_status"] = result.get("payment_status") or "Unpaid"
    result["fulfillment"] = json.loads(result.get("fulfillment") or "{}")
    result["quotation"] = json.loads(result.get("quotation") or "{}")
    result["payment_submissions"] = json.loads(result.get("payment_submissions") or "[]")
    result["quotation_history"] = json.loads(result.get("quotation_history") or "[]")
    for column, default in NEW_JSON_COLUMNS.items():
        result[column] = json.loads(result.get(column) or default)
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


def create_order(order: dict[str, Any], idempotency_key: str, audit: dict[str, Any] | None = None) -> str:
    """Insert an order and return its server-generated reference number.

    The reference is chosen inside an immediate write transaction, so concurrent submissions
    are serialized and cannot receive the same reference. ``audit`` is written in the same transaction.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reference = _reserve_reference(connection, order["created_at"])
        connection.execute(
            """
            INSERT INTO orders (
                reference, created_at, updated_at, customer, product, customization,
                event_details, has_event, acknowledged, status, total_amount,
                attachments, history, customer_id, idempotency_key, pricing, payment_option,
                fulfillment, quotation, payment_submissions, quotation_history,
                production, drafts, cancellation_requests, refunds, work_approval,
                required_down_payment, payment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # Fixed-price package orders store the package total; customized orders wait for a quotation.
                order.get("total_amount"),
                json.dumps(order["attachments"]),
                json.dumps(order["history"]),
                order.get("customer_id"),
                idempotency_key,
                json.dumps(order.get("pricing") or {}),
                # "" (chosen after the quotation) is kept; a missing value means the older default.
                _stored_payment_option(order.get("payment_option", "down_payment")),
                json.dumps(order.get("fulfillment") or {}),
                json.dumps(order.get("quotation") or {}),
                json.dumps(order.get("payment_submissions") or []),
                json.dumps(order.get("quotation_history") or []),
                json.dumps(order.get("production") or {}),
                json.dumps(order.get("drafts") or []),
                json.dumps(order.get("cancellation_requests") or []),
                json.dumps(order.get("refunds") or []),
                json.dumps(order.get("work_approval") or {}),
                order.get("required_down_payment"),
                order.get("payment_status") or "Unpaid",
            ),
        )
        if audit:
            audit_repository.insert(connection, reference, order["created_at"], audit)
    order["reference"] = reference
    return reference


def _stored_payment_option(value: object) -> str:
    return value if isinstance(value, str) else "down_payment"


class StaleOrderError(Exception):
    """Raised when an order changed after the caller read it (another action was saved first)."""


class ReferenceConflict(Exception):
    """A payment, refund or tracking reference was already claimed by another order."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or "That reference is already recorded.")


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
    if "payment_option" in updates:
        order["payment_option"] = updates["payment_option"]
    if "quotation" in updates:
        order["quotation"] = updates["quotation"]
    if "payment_submissions" in updates:
        order["payment_submissions"] = updates["payment_submissions"]
    if "quotation_history" in updates:
        order["quotation_history"] = updates["quotation_history"]
    if "fulfillment" in updates:
        order["fulfillment"] = updates["fulfillment"]
    if "production" in updates:
        order["production"] = updates["production"]
    if "drafts" in updates:
        order["drafts"] = updates["drafts"]
    if "cancellation_requests" in updates:
        order["cancellation_requests"] = updates["cancellation_requests"]
    if "refunds" in updates:
        order["refunds"] = updates["refunds"]
    if "work_approval" in updates:
        order["work_approval"] = updates["work_approval"]
    if "required_down_payment" in updates:
        order["required_down_payment"] = updates["required_down_payment"]
    if "payment_status" in updates:
        order["payment_status"] = updates["payment_status"]


def update_order(
    reference: str,
    updates: OrderUpdates,
    event: str,
    expected_version: int | None = None,
    audit: dict[str, Any] | list[dict[str, Any]] | None = None,
    notifications: list[dict[str, Any]] | None = None,
    claim_ops: list[dict[str, Any]] | None = None,
) -> OrderRecord | None:
    """Apply ``updates`` and append ``event`` to the history in one write transaction.

    When ``expected_version`` is given, the update is refused with StaleOrderError if the order was
    changed in the meantime, so a double-submitted or out-of-date action is never applied twice.

    ``audit`` ({"action", "actor_type", "actor_id", "details"}) is written to the audit log and each of
    ``notifications`` to the customer's notifications, in the same transaction. A list of audit entries
    writes them all in that same transaction, so an OCR correction and the payment it belongs to cannot
    be recorded separately. A notification whose ``dedupe_key`` was already used is skipped; each dict
    gets ``"created": True | False``.

    ``claim_ops`` reserves/releases payment, refund and tracking references in the same transaction,
    so a concurrent request can never save the same reference on two orders.
    """
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = decode_row(connection.execute("SELECT * FROM orders WHERE reference = ?", (reference,)).fetchone())
        if current is None:
            return None
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1
        ):
            raise StaleOrderError(reference)
        if expected_version is not None and order_version(current) != expected_version:
            raise StaleOrderError(reference)
        if claim_ops:
            try:
                reference_claim_repository.apply(connection, claim_ops, reference, current["updated_at"])
            except reference_claim_repository.DuplicateReference as error:
                raise ReferenceConflict(error.detail) from error
        previous_status = current["status"]
        previous_total = current["total_amount"]
        _apply_updates(current, updates)
        current["updated_at"] = now_iso()
        current["history"].append({"at": current["updated_at"], "text": event})
        # The first completion date is kept: an order completed again after a reprint stays reported on the
        # date it was first completed, so historical reports do not change.
        if current["status"] == "Completed" and previous_status != "Completed" and not current.get("completed_at"):
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
            "payment_option": _stored_payment_option(current.get("payment_option", "down_payment")),
            "quotation": json.dumps(current.get("quotation") or {}),
            "payment_submissions": json.dumps(current.get("payment_submissions") or []),
            "quotation_history": json.dumps(current.get("quotation_history") or []),
            "fulfillment": json.dumps(current.get("fulfillment") or {}),
            "production": json.dumps(current.get("production") or {}),
            "drafts": json.dumps(current.get("drafts") or []),
            "cancellation_requests": json.dumps(current.get("cancellation_requests") or []),
            "refunds": json.dumps(current.get("refunds") or []),
            "work_approval": json.dumps(current.get("work_approval") or {}),
            "required_down_payment": current.get("required_down_payment"),
            "payment_status": current.get("payment_status") or "Unpaid",
        }
        connection.execute(
            """
            UPDATE orders SET updated_at = :updated_at, status = :status,
                total_amount = :total_amount, payment_instructions = :payment_instructions,
                payments = :payments, attachments = :attachments,
                draft_attachment = :draft_attachment, history = :history,
                admin_notes = :admin_notes, completed_at = :completed_at,
                payment_option = :payment_option, quotation = :quotation,
                payment_submissions = :payment_submissions, quotation_history = :quotation_history,
                fulfillment = :fulfillment, production = :production, drafts = :drafts,
                cancellation_requests = :cancellation_requests, refunds = :refunds,
                work_approval = :work_approval,
                required_down_payment = :required_down_payment, payment_status = :payment_status
            WHERE reference = :reference
            """,
            {**values, "reference": reference},
        )
        audits = audit if isinstance(audit, list) else ([audit] if audit else [])
        for entry in audits:
            details = dict(entry.get("details") or {})
            if current["status"] != previous_status:
                details.setdefault("status_from", previous_status)
                details.setdefault("status_to", current["status"])
            if current["total_amount"] != previous_total:
                details.setdefault("total_from", previous_total)
                details.setdefault("total_to", current["total_amount"])
            audit_repository.insert(connection, reference, current["updated_at"], {**entry, "details": details})
        for notification in notifications or []:
            notification["created"] = notification_repository.insert(connection, reference, current["updated_at"], notification)
    return current
