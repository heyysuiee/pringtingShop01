"""Shop-made designs and their uploaded preview images.

``shop_designs`` holds the designs customers choose from on the order page ("Shop Design"). Uploaded
design images are stored in ``shop_design_images`` (in the database, not on disk), so they survive on
hosts with temporary disks (Vercel) and are shared by every copy of the app through Turso.

An image used by an order is *pinned*: it is never deleted, even when the design is edited, replaced, or
deleted, so historical orders always show the design that was ordered. Unused images of a deleted or
replaced design are removed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from backend.app.database.connection import connection_scope
from backend.app.database.design_seed import SAMPLE_DESIGNS
from backend.app.utils.clock import now_iso


SEED_MARKER = "shop_design_samples_v1"
IMAGE_URL_PREFIX = "/api/designs/images/"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_designs (
        id TEXT PRIMARY KEY,
        category_id TEXT NOT NULL,
        product_ids TEXT NOT NULL DEFAULT '[]',
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        style TEXT NOT NULL DEFAULT '',
        image TEXT NOT NULL DEFAULT '',
        fields TEXT NOT NULL DEFAULT '[]',
        is_sample INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shop_designs_category ON shop_designs(category_id)",
    """
    CREATE TABLE IF NOT EXISTS shop_design_images (
        id TEXT PRIMARY KEY,
        mime_type TEXT NOT NULL,
        data BLOB NOT NULL,
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
)

COLUMNS = ("category_id", "product_ids", "name", "description", "style", "image", "fields", "is_sample", "active", "sort_order")


def new_id() -> str:
    return f"dsg_{secrets.token_hex(8)}"


def initialize() -> None:
    with connection_scope() as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        if connection.execute("SELECT value FROM app_meta WHERE key = ?", (SEED_MARKER,)).fetchone() is None:
            # Seeded once. Samples the administrator deletes are not created again.
            for index, design in enumerate(SAMPLE_DESIGNS):
                if connection.execute("SELECT 1 FROM shop_designs WHERE id = ?", (design["id"],)).fetchone() is None:
                    _insert(connection, {**design, "sort_order": index})
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (SEED_MARKER, now_iso()))


def _json_list(value: Any) -> list[Any]:
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return loaded if isinstance(loaded, list) else []


def _from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "category_id": row["category_id"],
        "product_ids": [item for item in _json_list(row["product_ids"]) if isinstance(item, str)],
        "name": row["name"],
        "description": row["description"],
        "style": row["style"],
        "image": row["image"],
        "fields": [item for item in _json_list(row["fields"]) if isinstance(item, dict)],
        "is_sample": bool(row["is_sample"]),
        "active": bool(row["active"]),
        "sort_order": int(row["sort_order"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_designs(active_only: bool) -> list[dict[str, Any]]:
    where = " WHERE active = 1" if active_only else ""
    with connection_scope() as connection:
        rows = connection.execute(f"SELECT * FROM shop_designs{where} ORDER BY sort_order, created_at").fetchall()
    return [_from_row(row) for row in rows]


def get_design(design_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM shop_designs WHERE id = ?", (design_id,)).fetchone()
    return _from_row(row) if row else None


def next_sort_order() -> int:
    with connection_scope() as connection:
        return int(connection.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM shop_designs").fetchone()[0])


def _db_value(column: str, value: Any) -> Any:
    if column in ("product_ids", "fields"):
        return json.dumps(value)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _insert(connection: sqlite3.Connection, data: dict[str, Any]) -> str:
    design_id = data.get("id") or new_id()
    now = now_iso()
    values = {
        "category_id": data["category_id"],
        "product_ids": data.get("product_ids", []),
        "name": data["name"],
        "description": data.get("description", ""),
        "style": data.get("style", ""),
        "image": data.get("image", ""),
        "fields": data.get("fields", []),
        "is_sample": bool(data.get("is_sample", False)),
        "active": bool(data.get("active", True)),
        "sort_order": int(data.get("sort_order", 0)),
    }
    connection.execute(
        f"INSERT INTO shop_designs (id, {', '.join(COLUMNS)}, created_at, updated_at) VALUES ({', '.join('?' * (len(COLUMNS) + 3))})",
        (design_id, *(_db_value(column, values[column]) for column in COLUMNS), now, now),
    )
    return str(design_id)


def create_design(data: dict[str, Any]) -> str:
    with connection_scope() as connection:
        return _insert(connection, data)


def update_design(design_id: str, updates: dict[str, Any]) -> None:
    assignments = [f"{column} = ?" for column in COLUMNS if column in updates]
    if not assignments:
        return
    parameters = [_db_value(column, updates[column]) for column in COLUMNS if column in updates]
    with connection_scope() as connection:
        connection.execute(
            f"UPDATE shop_designs SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
            (*parameters, now_iso(), design_id),
        )


def delete_designs(design_ids: list[str]) -> None:
    if not design_ids:
        return
    with connection_scope() as connection:
        for design_id in design_ids:
            connection.execute("DELETE FROM shop_designs WHERE id = ?", (design_id,))


# ---------------------------------------------------------------- images


def image_id_from_url(url: str) -> str | None:
    """The stored image id of an uploaded design image URL, or None for a bundled (static) image."""
    if isinstance(url, str) and url.startswith(IMAGE_URL_PREFIX):
        return url[len(IMAGE_URL_PREFIX):].split(".", 1)[0] or None
    return None


def save_image(mime_type: str, data: bytes, extension: str) -> str:
    image_id = secrets.token_hex(16)
    with connection_scope() as connection:
        connection.execute(
            "INSERT INTO shop_design_images (id, mime_type, data, pinned, created_at) VALUES (?, ?, ?, 0, ?)",
            (image_id, mime_type, data, now_iso()),
        )
    return f"{IMAGE_URL_PREFIX}{image_id}{extension}"


def get_image(image_id: str) -> tuple[str, bytes] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT mime_type, data FROM shop_design_images WHERE id = ?", (image_id,)).fetchone()
    return (row["mime_type"], bytes(row["data"])) if row else None


def pin_image(url: str) -> None:
    """Keep an uploaded image forever: an order was placed with it."""
    image_id = image_id_from_url(url)
    if image_id is None:
        return
    with connection_scope() as connection:
        connection.execute("UPDATE shop_design_images SET pinned = 1 WHERE id = ?", (image_id,))


def discard_image(url: str) -> None:
    """Delete an uploaded image that no order and no design uses any more."""
    image_id = image_id_from_url(url)
    if image_id is None:
        return
    with connection_scope() as connection:
        in_use = connection.execute("SELECT 1 FROM shop_designs WHERE image = ?", (url,)).fetchone()
        if in_use is None:
            connection.execute("DELETE FROM shop_design_images WHERE id = ? AND pinned = 0", (image_id,))
