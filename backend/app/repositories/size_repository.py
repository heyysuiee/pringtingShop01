"""Standard size price lists (for example the Tarpaulin sizes and their prices).

Table
-----
standard_sizes   one row per standard size of a service category: label, width x height and unit, the price
                 of one piece in that size, display order, and whether customers can choose it.

Products priced by "size" (``catalog_products.pricing_mode = 'size'``) offer the active sizes of their
category on the order form; the server prices an order from these rows only. Orders keep their own copy of
the size and price they were placed with, so editing or deactivating a size never changes existing orders.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from backend.app.database.catalog_seed import STANDARD_SIZE_SEED
from backend.app.database.connection import connection_scope
from backend.app.models.catalog import StandardSize
from backend.app.utils.clock import now_iso


SEED_MARKER = "standard_sizes_seed_v1"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS standard_sizes (
        id TEXT PRIMARY KEY,
        category_id TEXT NOT NULL REFERENCES catalog_categories(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        width REAL NOT NULL,
        height REAL NOT NULL,
        unit TEXT NOT NULL DEFAULT 'ft',
        price REAL NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (category_id, width, height, unit)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_standard_sizes_category ON standard_sizes(category_id)",
)

COLUMNS = ("category_id", "label", "width", "height", "unit", "price", "sort_order", "active")


def initialize() -> None:
    """Create the table and seed the published Tarpaulin price list once (only into existing categories)."""
    with connection_scope() as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        if connection.execute("SELECT 1 FROM app_meta WHERE key = ?", (SEED_MARKER,)).fetchone() is None:
            for index, size in enumerate(STANDARD_SIZE_SEED):
                category = connection.execute("SELECT 1 FROM catalog_categories WHERE id = ?", (size["category_id"],)).fetchone()
                exists = connection.execute(
                    "SELECT 1 FROM standard_sizes WHERE id = ? OR (category_id = ? AND width = ? AND height = ? AND unit = ?)",
                    (size["id"], size["category_id"], size["width"], size["height"], size["unit"]),
                ).fetchone()
                if category is not None and exists is None:
                    _insert(connection, {**size, "sort_order": index, "active": True})
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (SEED_MARKER, now_iso()))


def _from_row(row: sqlite3.Row) -> StandardSize:
    return {
        "id": row["id"],
        "category_id": row["category_id"],
        "label": row["label"],
        "width": float(row["width"]),
        "height": float(row["height"]),
        "unit": row["unit"],
        "price": round(float(row["price"]), 2),
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
    }


def _insert(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    now = now_iso()
    connection.execute(
        """
        INSERT INTO standard_sizes (id, category_id, label, width, height, unit, price, sort_order, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["id"], data["category_id"], data["label"], float(data["width"]), float(data["height"]), data["unit"],
            round(float(data["price"]), 2), int(data.get("sort_order", 0)), 1 if data.get("active", True) else 0, now, now,
        ),
    )


def list_sizes(category_id: str | None = None, *, active_only: bool) -> list[StandardSize]:
    query = "SELECT * FROM standard_sizes"
    clauses: list[str] = []
    params: list[Any] = []
    if category_id is not None:
        clauses.append("category_id = ?")
        params.append(category_id)
    if active_only:
        clauses.append("active = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY category_id, sort_order, width * height, label"
    with connection_scope() as connection:
        return [_from_row(row) for row in connection.execute(query, params).fetchall()]


def get_size(size_id: str) -> StandardSize | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM standard_sizes WHERE id = ?", (size_id,)).fetchone()
    return _from_row(row) if row else None


def id_taken(size_id: str) -> bool:
    with connection_scope() as connection:
        return connection.execute("SELECT 1 FROM standard_sizes WHERE id = ?", (size_id,)).fetchone() is not None


def duplicate_dimensions(category_id: str, width: float, height: float, unit: str, exclude_id: str = "") -> bool:
    with connection_scope() as connection:
        return connection.execute(
            "SELECT 1 FROM standard_sizes WHERE category_id = ? AND width = ? AND height = ? AND unit = ? AND id != ?",
            (category_id, width, height, unit, exclude_id),
        ).fetchone() is not None


def next_sort_order(category_id: str) -> int:
    with connection_scope() as connection:
        row = connection.execute("SELECT MAX(sort_order) FROM standard_sizes WHERE category_id = ?", (category_id,)).fetchone()
    return (row[0] + 1) if row and row[0] is not None else 0


def create_size(data: dict[str, Any]) -> None:
    with connection_scope() as connection:
        _insert(connection, data)


def update_size(size_id: str, updates: dict[str, Any]) -> None:
    allowed = {key: value for key, value in updates.items() if key in COLUMNS}
    if not allowed:
        return
    values = [1 if value is True else 0 if value is False else value for value in allowed.values()]
    assignments = ", ".join(f"{column} = ?" for column in allowed)
    with connection_scope() as connection:
        connection.execute(
            f"UPDATE standard_sizes SET {assignments}, updated_at = ? WHERE id = ?",
            (*values, now_iso(), size_id),
        )


def delete_size(size_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM standard_sizes WHERE id = ?", (size_id,))
