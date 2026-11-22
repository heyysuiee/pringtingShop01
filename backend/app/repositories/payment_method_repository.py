"""E-wallet payment methods the shop accepts (GCash, Maya, and any other provider the administrator adds).

Customers see only active methods, in display order. On first start the table is seeded once with GCash
(active, because the shop already accepts GCash) and Maya (inactive until the administrator adds the shop's
Maya account). No account numbers, names, or QR codes are invented: the administrator enters them in
Admin → Settings → Payment methods.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from backend.app.database.connection import connection_scope
from backend.app.utils.clock import now_iso


SEED_MARKER = "payment_methods.seeded"
SEED = (
    ("GCash", True, 1),
    ("Maya", False, 2),
)


def initialize() -> None:
    with connection_scope() as connection:
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
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


def new_id() -> str:
    return f"pm_{secrets.token_hex(6)}"


def _decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["active"] = bool(item["active"])
    item["qr_file"] = json.loads(item["qr_file"]) if item.get("qr_file") else None
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
                display_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                method_id, values["provider"], values["account_name"], values["account_number"], values["instructions"],
                json.dumps(values["qr_file"]) if values.get("qr_file") else None, int(values["active"]),
                values["display_order"], now, now,
            ),
        )
    return get_method(method_id)  # type: ignore[return-value]


def update(method_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    columns = ("provider", "account_name", "account_number", "instructions", "qr_file", "active", "display_order")
    changes = {key: values[key] for key in columns if key in values}
    if "qr_file" in changes:
        changes["qr_file"] = json.dumps(changes["qr_file"]) if changes["qr_file"] else None
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
