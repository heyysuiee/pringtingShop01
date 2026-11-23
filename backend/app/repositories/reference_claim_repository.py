"""Database-enforced uniqueness for payment, refund and tracking references.

The order stores payments, refunds and delivery attempts as JSON columns, so an application-level
"scan every order" check is not atomic: two concurrent requests can both pass it and save the same
reference. This table reserves a normalized reference inside the *same* BEGIN IMMEDIATE transaction
that saves the order change. The primary key is the authoritative guard; the service-level check
remains only to give the customer a friendly message.

A claim can be released (a rejected receipt or a request for a clearer receipt frees its reference)
or archived (after a reprint an old tracking number may not be reused), so existing business rules
are preserved. A receipt-file claim is taken only when the file backs a verified payment: one receipt
can be submitted more than once for review, but it can never credit two payments.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from backend.app.database.connection import connection_scope, exclusive_write


KIND_PAYMENT = "payment"
KIND_RECEIPT = "receipt"
KIND_REFUND = "refund"
KIND_REFUND_ID = "refund_id"
KIND_TRACKING = "tracking"


class DuplicateReference(Exception):
    """Raised inside the order transaction when a reference is already claimed."""

    def __init__(self, kind: str, key: str, detail: str = "") -> None:
        self.kind = kind
        self.key = key
        self.detail = detail
        super().__init__(detail or f"That {kind} reference is already recorded.")


def initialize_with(connection: sqlite3.Connection) -> None:
    """Create the claim table using a caller-owned connection (used by the order migration)."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reference_claims (
            kind TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            order_reference TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (kind, claim_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reference_claims_owner ON reference_claims (order_reference)"
    )


def initialize() -> None:
    with exclusive_write() as connection:
        initialize_with(connection)


def apply(
    connection: sqlite3.Connection,
    operations: list[Mapping[str, Any]] | None,
    order_reference: str,
    now: str,
) -> None:
    """Apply claim operations inside an existing write transaction.

    Operations are applied in order. Each dict has a ``kind`` and a normalized ``key`` plus:

    * ``op: "acquire"`` (default) - reserve the key, or reuse it when ``allow_context`` matches the
      stored owner and context. A taken key raises :class:`DuplicateReference`.
    * ``op: "release"`` - free the key when it belongs to this order.
    * ``op: "archive"`` - mark the key as belonging to an earlier cycle via ``context`` so it can no
      longer be reused by a later dispatch.
    """
    for operation in operations or []:
        kind = str(operation.get("kind") or "")
        key = str(operation.get("key") or "")
        if not kind or not key:
            continue
        action = str(operation.get("op") or "acquire")
        detail = str(operation.get("detail") or "")
        if action == "release":
            connection.execute(
                "DELETE FROM reference_claims WHERE kind = ? AND claim_key = ? AND order_reference = ?",
                (kind, key, order_reference),
            )
            continue
        if action == "archive":
            connection.execute(
                "UPDATE reference_claims SET context = ? WHERE kind = ? AND claim_key = ? AND order_reference = ?",
                (str(operation.get("context") or ""), kind, key, order_reference),
            )
            continue
        context = str(operation.get("context") or "")
        allow = operation.get("allow_context")
        row = connection.execute(
            "SELECT order_reference, context FROM reference_claims WHERE kind = ? AND claim_key = ?",
            (kind, key),
        ).fetchone()
        if row is not None:
            if allow is not None and row["order_reference"] == order_reference and row["context"] == allow:
                # Same parcel/order redelivery: update the claim instead of rejecting it.
                connection.execute(
                    "UPDATE reference_claims SET context = ?, created_at = ? WHERE kind = ? AND claim_key = ?",
                    (context, now, kind, key),
                )
                continue
            raise DuplicateReference(kind, key, detail)
        try:
            connection.execute(
                """
                INSERT INTO reference_claims (kind, claim_key, order_reference, context, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, key, order_reference, context, now),
            )
        except sqlite3.IntegrityError as error:
            # The unique index is the final authority even if the SELECT above was bypassed.
            raise DuplicateReference(kind, key, detail) from error


def is_used(kind: str, key: str) -> bool:
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT 1 FROM reference_claims WHERE kind = ? AND claim_key = ?",
            (kind, key),
        ).fetchone()
    return row is not None
