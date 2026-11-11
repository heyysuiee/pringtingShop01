"""Database access for the product catalog and its per-product order-form configuration.

Tables
------
catalog_categories      one row per service category (Invitation, Souvenir, ...)
catalog_products        products inside a category, with the administrator's pricing settings
catalog_fields          the order-form questions for one product (size, material, uploads, ...)
catalog_field_options   the choices for select / radio / checkbox fields, each with its own price adjustment
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from backend.app.database.catalog_seed import CATALOG_SEED, slug
from backend.app.database.connection import connection_scope
from backend.app.models.catalog import CatalogCategory, CatalogField, CatalogProduct, FieldOption
from backend.app.utils.clock import now_iso
from backend.app.utils.images import optimized_image


SEED_MARKER = "catalog_seed_v1"
# Marks the sectioned Invitation order-form fields. Existing databases get their old invitation
# fields replaced once so the new package/customize flow has the right questions.
FIELD_MIGRATION_MARKER = "invitation_fields_v2"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_categories (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_products (
        id TEXT PRIMARY KEY,
        category_id TEXT NOT NULL REFERENCES catalog_categories(id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        image TEXT NOT NULL DEFAULT '',
        pricing_mode TEXT NOT NULL DEFAULT 'unit',
        base_price REAL,
        min_quantity INTEGER NOT NULL DEFAULT 1,
        max_quantity INTEGER NOT NULL DEFAULT 10000,
        event_mode TEXT NOT NULL DEFAULT 'none',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_fields (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL REFERENCES catalog_products(id) ON DELETE CASCADE,
        field_key TEXT NOT NULL,
        label TEXT NOT NULL,
        field_type TEXT NOT NULL,
        required INTEGER NOT NULL DEFAULT 0,
        placeholder TEXT NOT NULL DEFAULT '',
        help_text TEXT NOT NULL DEFAULT '',
        default_value TEXT NOT NULL DEFAULT '',
        min_value REAL,
        max_value REAL,
        max_length INTEGER,
        max_files INTEGER NOT NULL DEFAULT 1,
        depends_on TEXT NOT NULL DEFAULT '',
        depends_values TEXT NOT NULL DEFAULT '[]',
        section TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (product_id, field_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_field_options (
        id TEXT PRIMARY KEY,
        field_id TEXT NOT NULL REFERENCES catalog_fields(id) ON DELETE CASCADE,
        value TEXT NOT NULL,
        label TEXT NOT NULL,
        price_adjustment REAL NOT NULL DEFAULT 0,
        price_type TEXT NOT NULL DEFAULT 'per_unit',
        requires_quote INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (field_id, value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_catalog_products_category ON catalog_products(category_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_fields_product ON catalog_fields(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_options_field ON catalog_field_options(field_id)",
)


def initialize() -> None:
    with connection_scope() as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        _ensure_section_column(connection)
        seeded = connection.execute("SELECT value FROM app_meta WHERE key = ?", (SEED_MARKER,)).fetchone()
        if seeded is None:
            # Seed only once. If the administrator later removes products, they are not re-created.
            if connection.execute("SELECT COUNT(*) FROM catalog_categories").fetchone()[0] == 0:
                _seed(connection)
                # Fresh databases already have the sectioned invitation fields.
                connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (FIELD_MIGRATION_MARKER, now_iso()))
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (SEED_MARKER, now_iso()))
        _migrate_invitation_fields(connection)


def _ensure_section_column(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(catalog_fields)")}
    if "section" not in columns:
        connection.execute("ALTER TABLE catalog_fields ADD COLUMN section TEXT NOT NULL DEFAULT ''")


def _migrate_invitation_fields(connection: sqlite3.Connection) -> None:
    """Replace the old Invitation form fields with the sectioned package/customize fields once."""
    if connection.execute("SELECT value FROM app_meta WHERE key = ?", (FIELD_MIGRATION_MARKER,)).fetchone():
        return
    for category in CATALOG_SEED:
        if category["id"] != "invitation":
            continue
        for product in category["products"]:
            if connection.execute("SELECT 1 FROM catalog_products WHERE id = ?", (product["id"],)).fetchone() is None:
                continue
            connection.execute("DELETE FROM catalog_fields WHERE product_id = ?", (product["id"],))
            for field_index, field in enumerate(product["fields"]):
                _insert_field(connection, product["id"], {**field, "sort_order": field_index})
    connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?)", (FIELD_MIGRATION_MARKER, now_iso()))


def _seed(connection: sqlite3.Connection) -> None:
    for category_index, category in enumerate(CATALOG_SEED):
        _insert_category(connection, {**category, "sort_order": category_index})
        for product_index, product in enumerate(category["products"]):
            _insert_product(connection, {**product, "category_id": category["id"], "sort_order": product_index})
            for field_index, field in enumerate(product["fields"]):
                _insert_field(connection, product["id"], {**field, "sort_order": field_index})


# ------------------------------------------------------------------ row decoding


def _option_from_row(row: sqlite3.Row) -> FieldOption:
    return {
        "id": row["id"],
        "field_id": row["field_id"],
        "value": row["value"],
        "label": row["label"],
        "price_adjustment": float(row["price_adjustment"]),
        "price_type": row["price_type"],
        "requires_quote": bool(row["requires_quote"]),
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
    }


def _field_from_row(row: sqlite3.Row) -> CatalogField:
    try:
        depends_values = json.loads(row["depends_values"] or "[]")
    except (TypeError, ValueError):
        depends_values = []
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "field_key": row["field_key"],
        "label": row["label"],
        "field_type": row["field_type"],
        "required": bool(row["required"]),
        "placeholder": row["placeholder"],
        "help_text": row["help_text"],
        "default_value": row["default_value"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
        "max_length": row["max_length"],
        "max_files": int(row["max_files"]),
        "depends_on": row["depends_on"],
        "depends_values": [str(value) for value in depends_values if isinstance(value, str)],
        "section": row["section"] if "section" in row.keys() else "",
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
        "options": [],
    }


def _product_from_row(row: sqlite3.Row, category_label: str) -> CatalogProduct:
    return {
        "id": row["id"],
        "category_id": row["category_id"],
        "category_label": category_label,
        "name": row["name"],
        "description": row["description"],
        "image": optimized_image(row["image"]),
        "pricing_mode": row["pricing_mode"],
        "base_price": None if row["base_price"] is None else float(row["base_price"]),
        "min_quantity": int(row["min_quantity"]),
        "max_quantity": int(row["max_quantity"]),
        "event_mode": row["event_mode"],
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
        "fields": [],
    }


def _category_from_row(row: sqlite3.Row) -> CatalogCategory:
    return {
        "id": row["id"],
        "label": row["label"],
        "description": row["description"],
        "sort_order": int(row["sort_order"]),
        "active": bool(row["active"]),
        "products": [],
    }


# ------------------------------------------------------------------ reads


def load_catalog(active_only: bool) -> list[CatalogCategory]:
    """Return the full category → product → field → option tree in display order."""
    active = " WHERE active = 1" if active_only else ""
    with connection_scope() as connection:
        category_rows = connection.execute(f"SELECT * FROM catalog_categories{active} ORDER BY sort_order, label").fetchall()
        product_rows = connection.execute(f"SELECT * FROM catalog_products{active} ORDER BY sort_order, name").fetchall()
        field_rows = connection.execute(f"SELECT * FROM catalog_fields{active} ORDER BY sort_order, created_at").fetchall()
        option_rows = connection.execute(f"SELECT * FROM catalog_field_options{active} ORDER BY sort_order, created_at").fetchall()

    categories = [_category_from_row(row) for row in category_rows]
    by_category = {category["id"]: category for category in categories}
    products: dict[str, CatalogProduct] = {}
    for row in product_rows:
        category = by_category.get(row["category_id"])
        if category is None:
            continue
        product = _product_from_row(row, category["label"])
        category["products"].append(product)
        products[product["id"]] = product
    fields: dict[str, CatalogField] = {}
    for row in field_rows:
        owner = products.get(row["product_id"])
        if owner is None:
            continue
        field = _field_from_row(row)
        owner["fields"].append(field)
        fields[field["id"]] = field
    for row in option_rows:
        owner_field = fields.get(row["field_id"])
        if owner_field is not None:
            owner_field["options"].append(_option_from_row(row))
    return categories


def list_categories() -> list[dict[str, Any]]:
    """Every service category (shown or hidden) in display order, without products."""
    with connection_scope() as connection:
        rows = connection.execute("SELECT id, label, active FROM catalog_categories ORDER BY sort_order, label").fetchall()
    return [{"id": row["id"], "label": row["label"], "active": bool(row["active"])} for row in rows]


def get_product(product_id: str, active_only: bool) -> CatalogProduct | None:
    """Load one product with its fields and options. With active_only, the category must be active too."""
    with connection_scope() as connection:
        row = connection.execute(
            """
            SELECT p.*, c.label AS category_label, c.active AS category_active
            FROM catalog_products p JOIN catalog_categories c ON c.id = p.category_id
            WHERE p.id = ?
            """,
            (product_id,),
        ).fetchone()
        if row is None or (active_only and not (row["active"] and row["category_active"])):
            return None
        product = _product_from_row(row, row["category_label"])
        active = " AND active = 1" if active_only else ""
        field_rows = connection.execute(
            f"SELECT * FROM catalog_fields WHERE product_id = ?{active} ORDER BY sort_order, created_at",
            (product_id,),
        ).fetchall()
        fields = [_field_from_row(field_row) for field_row in field_rows]
        if fields:
            placeholders = ",".join("?" for _ in fields)
            option_rows = connection.execute(
                f"SELECT * FROM catalog_field_options WHERE field_id IN ({placeholders}){active} ORDER BY sort_order, created_at",
                [field["id"] for field in fields],
            ).fetchall()
            by_field = {field["id"]: field for field in fields}
            for option_row in option_rows:
                by_field[option_row["field_id"]]["options"].append(_option_from_row(option_row))
    product["fields"] = fields
    return product


def get_category(category_id: str) -> CatalogCategory | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM catalog_categories WHERE id = ?", (category_id,)).fetchone()
    return _category_from_row(row) if row else None


def get_field(field_id: str) -> CatalogField | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM catalog_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            return None
        field = _field_from_row(row)
        option_rows = connection.execute(
            "SELECT * FROM catalog_field_options WHERE field_id = ? ORDER BY sort_order, created_at",
            (field_id,),
        ).fetchall()
    field["options"] = [_option_from_row(option_row) for option_row in option_rows]
    return field


def unique_id(table: str, base: str) -> str:
    """Return ``base`` or ``base-2``, ``base-3``... so slugs used in URLs stay unique."""
    if table not in {"catalog_categories", "catalog_products"}:
        raise ValueError("Unsupported table.")
    candidate = base
    with connection_scope() as connection:
        sequence = 1
        while connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone():
            sequence += 1
            candidate = f"{base}-{sequence}"
    return candidate


def next_sort_order(table: str, parent_column: str, parent_id: str) -> int:
    if (table, parent_column) not in {("catalog_products", "category_id"), ("catalog_fields", "product_id")}:
        raise ValueError("Unsupported table.")
    with connection_scope() as connection:
        value = connection.execute(
            f"SELECT COALESCE(MAX(sort_order), -1) + 1 FROM {table} WHERE {parent_column} = ?",
            (parent_id,),
        ).fetchone()[0]
    return int(value)


def count_products(category_id: str) -> int:
    with connection_scope() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM catalog_products WHERE category_id = ?", (category_id,)).fetchone()[0])


# ------------------------------------------------------------------ inserts


def _insert_category(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    now = now_iso()
    connection.execute(
        """
        INSERT INTO catalog_categories (id, label, description, sort_order, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (data["id"], data["label"], data.get("description", ""), int(data.get("sort_order", 0)), 1 if data.get("active", True) else 0, now, now),
    )


def _insert_product(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    now = now_iso()
    connection.execute(
        """
        INSERT INTO catalog_products (
            id, category_id, name, description, image, pricing_mode, base_price,
            min_quantity, max_quantity, event_mode, sort_order, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["id"],
            data["category_id"],
            data["name"],
            data.get("description", ""),
            data.get("image", ""),
            data.get("pricing_mode", "unit"),
            data.get("base_price"),
            int(data.get("min_quantity", 1)),
            int(data.get("max_quantity", 10000)),
            data.get("event_mode", "none"),
            int(data.get("sort_order", 0)),
            1 if data.get("active", True) else 0,
            now,
            now,
        ),
    )


def _insert_field(connection: sqlite3.Connection, product_id: str, data: dict[str, Any]) -> str:
    now = now_iso()
    field_id = data.get("id") or new_id("fld")
    connection.execute(
        """
        INSERT INTO catalog_fields (
            id, product_id, field_key, label, field_type, required, placeholder, help_text, default_value,
            min_value, max_value, max_length, max_files, depends_on, depends_values, section, sort_order, active,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            field_id,
            product_id,
            data["field_key"],
            data["label"],
            data["field_type"],
            1 if data.get("required") else 0,
            data.get("placeholder", ""),
            data.get("help_text", ""),
            data.get("default_value", ""),
            data.get("min_value"),
            data.get("max_value"),
            data.get("max_length"),
            int(data.get("max_files", 1) or 1),
            data.get("depends_on", ""),
            json.dumps(list(data.get("depends_values", []))),
            data.get("section", ""),
            int(data.get("sort_order", 0)),
            1 if data.get("active", True) else 0,
            now,
            now,
        ),
    )
    for index, option in enumerate(data.get("options", [])):
        _insert_option(connection, field_id, {**option, "sort_order": option.get("sort_order", index)})
    return field_id


def _insert_option(connection: sqlite3.Connection, field_id: str, data: dict[str, Any]) -> None:
    now = now_iso()
    connection.execute(
        """
        INSERT INTO catalog_field_options (
            id, field_id, value, label, price_adjustment, price_type, requires_quote, sort_order, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("id") or new_id("opt"),
            field_id,
            data.get("value") or slug(data["label"]),
            data["label"],
            float(data.get("price_adjustment", 0) or 0),
            data.get("price_type", "per_unit"),
            1 if data.get("requires_quote") else 0,
            int(data.get("sort_order", 0)),
            1 if data.get("active", True) else 0,
            now,
            now,
        ),
    )


# ------------------------------------------------------------------ writes used by the admin API


def create_category(data: dict[str, Any]) -> None:
    with connection_scope() as connection:
        _insert_category(connection, data)


def update_category(category_id: str, updates: dict[str, Any]) -> None:
    _update_row("catalog_categories", category_id, updates, ("label", "description", "sort_order", "active"))


def delete_category(category_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM catalog_categories WHERE id = ?", (category_id,))


def create_product(data: dict[str, Any]) -> None:
    with connection_scope() as connection:
        _insert_product(connection, data)


def update_product(product_id: str, updates: dict[str, Any]) -> None:
    _update_row(
        "catalog_products",
        product_id,
        updates,
        ("category_id", "name", "description", "image", "pricing_mode", "base_price", "min_quantity", "max_quantity", "event_mode", "sort_order", "active"),
    )


def delete_product(product_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM catalog_products WHERE id = ?", (product_id,))


def create_field(product_id: str, data: dict[str, Any]) -> str:
    with connection_scope() as connection:
        return _insert_field(connection, product_id, data)


def update_field(field_id: str, updates: dict[str, Any], options: list[dict[str, Any]] | None) -> None:
    """Update a field. When ``options`` is given it replaces the field's option list.

    Options keep their id when the payload includes it, so existing choices are updated in place;
    options missing from the list are deleted. Orders keep their own copy of what was chosen.
    """
    columns = (
        "field_key", "label", "field_type", "required", "placeholder", "help_text", "default_value",
        "min_value", "max_value", "max_length", "max_files", "depends_on", "depends_values", "section", "active",
    )
    values = dict(updates)
    if "depends_values" in values:
        values["depends_values"] = json.dumps(list(values["depends_values"]))
    with connection_scope() as connection:
        _update_row("catalog_fields", field_id, values, columns, connection)
        if options is None:
            return
        existing = {row["id"] for row in connection.execute("SELECT id FROM catalog_field_options WHERE field_id = ?", (field_id,))}
        keep: set[str] = set()
        # Free up values first so a reorder or rename cannot trip the (field_id, value) unique index.
        connection.execute("UPDATE catalog_field_options SET value = '__tmp_' || id WHERE field_id = ?", (field_id,))
        for index, option in enumerate(options):
            option_id = option.get("id")
            if option_id is not None and option_id in existing:
                keep.add(option_id)
                _update_row(
                    "catalog_field_options",
                    option_id,
                    {**option, "sort_order": index},
                    ("value", "label", "price_adjustment", "price_type", "requires_quote", "sort_order", "active"),
                    connection,
                )
            else:
                _insert_option(connection, field_id, {**option, "id": None, "sort_order": index})
        for option_id in existing - keep:
            connection.execute("DELETE FROM catalog_field_options WHERE id = ?", (option_id,))


def delete_field(field_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM catalog_fields WHERE id = ?", (field_id,))


def reorder_fields(product_id: str, field_ids: list[str]) -> None:
    with connection_scope() as connection:
        for index, field_id in enumerate(field_ids):
            connection.execute(
                "UPDATE catalog_fields SET sort_order = ?, updated_at = ? WHERE id = ? AND product_id = ?",
                (index, now_iso(), field_id, product_id),
            )


def update_pricing(product_id: str, product_updates: dict[str, Any], option_updates: list[dict[str, Any]]) -> None:
    """Apply a product's base price and its options' price adjustments in one transaction."""
    with connection_scope() as connection:
        _update_row("catalog_products", product_id, product_updates, ("pricing_mode", "base_price"), connection)
        for option in option_updates:
            owned = connection.execute(
                """
                SELECT 1 FROM catalog_field_options o JOIN catalog_fields f ON f.id = o.field_id
                WHERE o.id = ? AND f.product_id = ?
                """,
                (option["id"], product_id),
            ).fetchone()
            if owned:
                _update_row(
                    "catalog_field_options",
                    option["id"],
                    option,
                    ("price_adjustment", "price_type", "requires_quote"),
                    connection,
                )


def _update_row(
    table: str,
    row_id: str,
    updates: dict[str, Any],
    allowed: tuple[str, ...],
    connection: sqlite3.Connection | None = None,
) -> None:
    assignments = []
    values: list[Any] = []
    for column in allowed:
        if column not in updates:
            continue
        value = updates[column]
        if isinstance(value, bool):
            value = 1 if value else 0
        assignments.append(f"{column} = ?")
        values.append(value)
    if not assignments:
        return
    assignments.append("updated_at = ?")
    values.extend([now_iso(), row_id])
    statement = f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?"
    if connection is not None:
        connection.execute(statement, values)
        return
    with connection_scope() as own_connection:
        own_connection.execute(statement, values)
