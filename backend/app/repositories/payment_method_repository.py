"""Payment methods the shop accepts: e-wallets, online banking, QR payments, cash, and any other provider the
administrator adds.

Customers see only active methods, grouped by category, in display order. On first start the table is seeded
once with GCash (active, because the shop already accepts GCash) and Maya (inactive until the administrator adds
the shop's Maya account). A second one-time seed adds common Philippine options (BDO, BPI, UnionBank, QR Ph, and
cash at the counter) **inactive**: no account numbers, names, or QR codes are invented, so none of them can be
offered to customers until the administrator enters the shop's real details in Admin → Settings → Payment methods.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any

from backend.app.database.connection import connection_scope, exclusive_write
from backend.app.utils.clock import now_iso


SEED_MARKER = "payment_methods.seeded"
SEED = (
    ("GCash", True, 1),
    ("Maya", False, 2),
)
CATALOG_MARKER = "payment_methods.catalog_seeded"
# (provider, category, display order, short description). Descriptions explain the channel only.
CATALOG_SEED = (
    ("BDO", "bank", 10, "Online or mobile banking transfer, InstaPay, or PESONet to the shop's BDO account."),
    ("BPI", "bank", 11, "Online or mobile banking transfer, InstaPay, or PESONet to the shop's BPI account."),
    ("UnionBank", "bank", 12, "Online or mobile banking transfer, InstaPay, or PESONet to the shop's UnionBank account."),
    ("QR Ph", "qr", 20, "Scan the shop's QR Ph code with any participating bank or e-wallet app."),
    ("Cash", "cash", 30, "Pay in person at the shop counter. The shop records the payment and issues an official receipt."),
)
EWALLET_SEED = (
    ("MariBank", "ewallet", 3, "Pay through the shop's MariBank account and upload the transaction receipt for verification."),
)
CATEGORIES = {
    "ewallet": "E-Wallets",
    "bank": "Online Banking",
    "qr": "QR Payments",
    "cash": "Cash / Over-the-Counter",
    "other": "Other Payment Options",
}
_BANK_NAMES = re.compile(r"\b(bank|bdo|bpi|unionbank|landbank|metrobank|rcbc|chinabank|pnb|eastwest|security bank)\b", re.I)


def infer_category(provider: str) -> str:
    """A sensible category for a method created before categories existed (the administrator can change it)."""
    name = str(provider or "")
    if re.search(r"\bqr\s*ph\b|\bqrph\b", name, re.I):
        return "qr"
    if re.search(r"\bcash\b|over[- ]the[- ]counter", name, re.I):
        return "cash"
    if _BANK_NAMES.search(name):
        return "bank"
    return "ewallet"


def initialize() -> None:
    with exclusive_write() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_methods (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '',
                instructions TEXT NOT NULL DEFAULT '',
                qr_file TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                display_order INTEGER NOT NULL DEFAULT 0,
                verification_method TEXT NOT NULL DEFAULT 'manual',
                gateway_config TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Additive migration for databases created before OCR-assisted verification.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(payment_methods)").fetchall()}
        if "verification_method" not in columns:
            connection.execute("ALTER TABLE payment_methods ADD COLUMN verification_method TEXT NOT NULL DEFAULT 'manual'")
        if "gateway_config" not in columns:
            connection.execute("ALTER TABLE payment_methods ADD COLUMN gateway_config TEXT NOT NULL DEFAULT '{}'")
        # Additive migration for the categorized payment catalog (logos, descriptions, categories).
        if "category" not in columns:
            connection.execute("ALTER TABLE payment_methods ADD COLUMN category TEXT NOT NULL DEFAULT 'ewallet'")
            for row in connection.execute("SELECT id, provider FROM payment_methods").fetchall():
                connection.execute("UPDATE payment_methods SET category = ? WHERE id = ?", (infer_category(row["provider"]), row["id"]))
        if "description" not in columns:
            connection.execute("ALTER TABLE payment_methods ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "logo_file" not in columns:
            connection.execute("ALTER TABLE payment_methods ADD COLUMN logo_file TEXT")
        connection.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if connection.execute("SELECT value FROM app_meta WHERE key = ?", (SEED_MARKER,)).fetchone() is None:
            now = now_iso()
            existing = {row["provider"].casefold() for row in connection.execute("SELECT provider FROM payment_methods")}
            for provider, active, order in SEED:
                if provider.casefold() in existing:
                    continue
                connection.execute(
                    "INSERT INTO payment_methods (id, provider, active, display_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id(), provider, int(active), order, now, now),
                )
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (SEED_MARKER, now))
        if connection.execute("SELECT value FROM app_meta WHERE key = ?", (CATALOG_MARKER,)).fetchone() is None:
            now = now_iso()
            existing = {row["provider"].casefold() for row in connection.execute("SELECT provider FROM payment_methods")}
            for provider, category, order, description in CATALOG_SEED:
                if provider.casefold() in existing:
                    continue
                # Inactive: the administrator must add the shop's own account details before customers see it.
                connection.execute(
                    "INSERT INTO payment_methods (id, provider, category, description, active, display_order, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                    (new_id(), provider, category, description, order, now, now),
                )
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (CATALOG_MARKER, now))

        # Additive support for newer e-wallet options without disturbing existing configured methods.
        now = now_iso()
        existing = {row["provider"].casefold() for row in connection.execute("SELECT provider FROM payment_methods")}
        for provider, category, order, description in EWALLET_SEED:
            if provider.casefold() in existing:
                continue
            connection.execute(
                "INSERT INTO payment_methods (id, provider, category, description, active, display_order, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (new_id(), provider, category, description, order, now, now),
            )


def new_id() -> str:
    return f"pm_{secrets.token_hex(6)}"


def _decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["active"] = bool(item["active"])
    item["qr_file"] = json.loads(item["qr_file"]) if item.get("qr_file") else None
    item["logo_file"] = json.loads(item["logo_file"]) if item.get("logo_file") else None
    if item.get("category") not in CATEGORIES:
        item["category"] = infer_category(item.get("provider", ""))
    item.setdefault("description", "")
    try:
        item["gateway_config"] = json.loads(item.get("gateway_config") or "{}")
    except (TypeError, ValueError):
        item["gateway_config"] = {}
    item.setdefault("verification_method", "manual")
    if not isinstance(item["gateway_config"], dict):
        item["gateway_config"] = {}
    return item


def list_methods(active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM payment_methods"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY display_order, provider"
    with connection_scope() as connection:
        rows = connection.execute(sql).fetchall()
    return [item for row in rows if (item := _decode(row)) is not None]


def get_method(method_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        return _decode(connection.execute("SELECT * FROM payment_methods WHERE id = ?", (method_id,)).fetchone())


def insert(values: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    method_id = new_id()
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO payment_methods (id, provider, account_name, account_number, instructions, qr_file, active,
                display_order, verification_method, gateway_config, category, description, logo_file, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                method_id, values["provider"], values["account_name"], values["account_number"], values["instructions"],
                json.dumps(values["qr_file"]) if values.get("qr_file") else None, int(values["active"]),
                values["display_order"], values.get("verification_method", "manual"),
                json.dumps(values.get("gateway_config") or {}),
                values.get("category") or infer_category(values["provider"]), values.get("description", ""),
                json.dumps(values["logo_file"]) if values.get("logo_file") else None, now, now,
            ),
        )
    return get_method(method_id)  # type: ignore[return-value]


def update(method_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    columns = (
        "provider", "account_name", "account_number", "instructions", "qr_file", "active", "display_order",
        "verification_method", "gateway_config", "category", "description", "logo_file",
    )
    changes = {key: values[key] for key in columns if key in values}
    for key in ("qr_file", "logo_file"):
        if key in changes:
            changes[key] = json.dumps(changes[key]) if changes[key] else None
    if "gateway_config" in changes:
        changes["gateway_config"] = json.dumps(changes["gateway_config"] or {})
    if "active" in changes:
        changes["active"] = int(bool(changes["active"]))
    changes["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    with connection_scope() as connection:
        connection.execute(f"UPDATE payment_methods SET {assignments} WHERE id = :id", {**changes, "id": method_id})
    return get_method(method_id)


def delete(method_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM payment_methods WHERE id = ?", (method_id,))
