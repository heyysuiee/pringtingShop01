"""Ready-made packages (sets) shown on the customer order page.

Packages started with the three Invitation categories and now exist for every product: each package
belongs to one product and carries its published price, what is included, its images, and an optional
add-on charge for extra pieces beyond the included quantity. The table keeps its original name
(``invitation_packages``) so existing databases keep their data.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any, cast

from backend.app.database.catalog_seed import INVITATION_PACKAGES
from backend.app.database.connection import connection_scope
from backend.app.models.catalog import InvitationPackage
from backend.app.utils.clock import now_iso
from backend.app.utils.images import optimized_image


SEED_MARKER = "invitation_packages_v1"

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS invitation_packages (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL REFERENCES catalog_products(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        size TEXT NOT NULL DEFAULT '',
        material TEXT NOT NULL DEFAULT '',
        inclusions TEXT NOT NULL DEFAULT '[]',
        add_on_label TEXT NOT NULL DEFAULT '',
        add_on_price REAL,
        included_quantity INTEGER NOT NULL DEFAULT 20,
        image TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invitation_packages_product ON invitation_packages(product_id)",
)


def new_id() -> str:
    return f"pkg_{secrets.token_hex(8)}"


def initialize() -> None:
    with connection_scope() as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(invitation_packages)")}
        if "images" not in columns:
            # Every package image in display order (JSON list of site paths); ``image`` is the first.
            connection.execute("ALTER TABLE invitation_packages ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")
        if "description" not in columns:
            connection.execute("ALTER TABLE invitation_packages ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        seeded = connection.execute("SELECT value FROM app_meta WHERE key = ?", (SEED_MARKER,)).fetchone()
        if seeded is None:
            _seed(connection)
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (SEED_MARKER, now_iso()))


def _seed(connection: sqlite3.Connection) -> None:
    for package in INVITATION_PACKAGES:
        # A product the administrator removed is simply skipped; the package is not re-created.
        if connection.execute("SELECT 1 FROM catalog_products WHERE id = ?", (package["product_id"],)).fetchone() is None:
            continue
        _insert(connection, package)


def _images(row: sqlite3.Row) -> list[str]:
    try:
        values = json.loads(row["images"] or "[]") if "images" in row.keys() else []
    except (TypeError, ValueError):
        values = []
    images = [value for value in values if isinstance(value, str) and value] if isinstance(values, list) else []
    if not images and row["image"]:
        images = [row["image"]]
    return [optimized_image(path) for path in images]


def _package_from_row(row: sqlite3.Row) -> InvitationPackage:
    try:
        inclusions = json.loads(row["inclusions"] or "[]")
    except (TypeError, ValueError):
        inclusions = []
    images = _images(row)
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "name": row["name"],
        "description": row["description"] if "description" in row.keys() else "",
        "price": float(row["price"]),
        "size": row["size"],
        "material": row["material"],
        "inclusions": [str(item) for item in inclusions if isinstance(item, str)],
        "add_on_label": row["add_on_label"],
        "add_on_price": None if row["add_on_price"] is None else float(row["add_on_price"]),
        "included_quantity": int(row["included_quantity"]),
        "image": images[0] if images else "",
        "images": images,
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
    }


def list_packages(product_id: str, active_only: bool) -> list[InvitationPackage]:
    active = " AND active = 1" if active_only else ""
    with connection_scope() as connection:
        rows = connection.execute(
            f"SELECT * FROM invitation_packages WHERE product_id = ?{active} ORDER BY sort_order, created_at",
            (product_id,),
        ).fetchall()
    return [_package_from_row(row) for row in rows]


def list_all_packages(active_only: bool) -> list[InvitationPackage]:
    """Every package of every product, grouped by product in display order."""
    active = " WHERE active = 1" if active_only else ""
    with connection_scope() as connection:
        rows = connection.execute(f"SELECT * FROM invitation_packages{active} ORDER BY product_id, sort_order, created_at").fetchall()
    return [_package_from_row(row) for row in rows]


def get_package(package_id: str) -> InvitationPackage | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM invitation_packages WHERE id = ?", (package_id,)).fetchone()
    return _package_from_row(row) if row else None


def next_sort_order(product_id: str) -> int:
    with connection_scope() as connection:
        value = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM invitation_packages WHERE product_id = ?",
            (product_id,),
        ).fetchone()[0]
    return int(value)


def _insert(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    now = now_iso()
    images = [path for path in (data.get("images") or ([data["image"]] if data.get("image") else [])) if path]
    connection.execute(
        """
        INSERT INTO invitation_packages (
            id, product_id, name, description, price, size, material, inclusions, add_on_label, add_on_price,
            included_quantity, image, images, sort_order, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("id") or new_id(),
            data["product_id"],
            data["name"],
            data.get("description", ""),
            float(data.get("price", 0) or 0),
            data.get("size", ""),
            data.get("material", ""),
            json.dumps(list(data.get("inclusions", []))),
            data.get("add_on_label", ""),
            data.get("add_on_price"),
            int(data.get("included_quantity", 20) or 20),
            images[0] if images else "",
            json.dumps(images),
            int(data.get("sort_order", 0)),
            1 if data.get("active", True) else 0,
            now,
            now,
        ),
    )


def create_package(data: dict[str, Any]) -> str:
    with connection_scope() as connection:
        package_id = data.get("id") or new_id()
        _insert(connection, {**data, "id": package_id})
    return cast(str, package_id)


def update_package(package_id: str, updates: dict[str, Any]) -> None:
    columns = (
        "product_id", "name", "description", "price", "size", "material", "inclusions", "add_on_label", "add_on_price",
        "included_quantity", "image", "images", "sort_order", "active",
    )
    values = dict(updates)
    if "inclusions" in values:
        values["inclusions"] = json.dumps(list(values["inclusions"]))
    if "images" in values:
        images = [path for path in values["images"] if path]
        values["images"] = json.dumps(images)
        values["image"] = images[0] if images else ""
    assignments = []
    parameters: list[Any] = []
    for column in columns:
        if column not in values:
            continue
        value = values[column]
        if isinstance(value, bool):
            value = 1 if value else 0
        assignments.append(f"{column} = ?")
        parameters.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    parameters.extend([now_iso(), package_id])
    with connection_scope() as connection:
        connection.execute(f"UPDATE invitation_packages SET {', '.join(assignments)} WHERE id = ?", parameters)


def delete_package(package_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM invitation_packages WHERE id = ?", (package_id,))
