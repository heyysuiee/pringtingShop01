"""Database backup and restore for administrators.

Works the same for the local SQLite file and for Turso (hosted SQLite on Vercel): a backup is a JSON
document with the rows of every application table, read through the normal database connection, so no
database file path or credential is ever exposed or needed.

* **Backup** contains the shop's data: customers, the catalog and packages, orders (with their payments,
  submissions, quotations, and history), the audit log, customer notifications, and app settings. It
  leaves out administrator credentials, password-reset codes, and sign-in throttling records, so a
  restore can never change the administrator's password or lock anyone out.
* Each file carries a SHA-256 checksum of its data and an HMAC signature made with the server's
  ``PRINTSHOP_SECRET_KEY``. A restore accepts only files signed by this installation and unchanged
  since they were created, so an edited or foreign file cannot be loaded into the database.
* **Restore** validates the whole file first (format, tables, columns, value types, JSON fields), then
  replaces the tables inside one transaction: either every table is restored or nothing changes.
* Backup and restore events (time, administrator, result, row counts) are kept in ``app_meta``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.database.connection import connection_scope
from backend.app.repositories import admin_repository


logger = logging.getLogger("printshop.backup")

FORMAT = "impression-printshop-backup"
VERSION = 1
# Restored in this order (parents before the tables that reference them); cleared in reverse.
BACKUP_TABLES = (
    "app_meta",
    "customer_accounts",
    "catalog_categories",
    "catalog_products",
    "catalog_fields",
    "catalog_field_options",
    "standard_sizes",
    "payment_methods",
    "invitation_packages",
    "shop_designs",
    "shop_design_images",
    "orders",
    "order_audit_log",
    "customer_notifications",
)
# Tables added after backups were first made. A backup without them is still restored; this installation's
# rows of such a table are kept (those that still belong to a restored category).
OPTIONAL_TABLES = ("standard_sizes", "payment_methods")
# Columns holding JSON text that the application decodes; checked before a restore.
JSON_COLUMNS = {
    "orders": ("customer", "product", "customization", "event_details", "payments", "attachments", "history",
               "pricing", "fulfillment", "quotation", "payment_submissions", "quotation_history",
               "production", "drafts", "cancellation_requests", "refunds"),
    "payment_methods": ("qr_file",),
    "order_audit_log": ("details",),
    "invitation_packages": ("inclusions", "images"),
    "shop_designs": ("product_ids", "fields"),
}
# app_meta keys that belong to this installation (notification read state, backup history): never
# written into a backup and never replaced by a restore.
LOCAL_META_PREFIXES = admin_repository.SETTING_PREFIXES
HISTORY_KEY = "backup.history"
HISTORY_LIMIT = 20
MAX_BACKUP_BYTES = 25 * 1024 * 1024
RESTORE_CONFIRMATION = "RESTORE"


# ================================================================== helpers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _database_kind() -> str:
    return "turso" if settings.turso_database_url else "sqlite"


def _normalize(value: Any) -> Any:
    """Whole-number floats as integers, so 450.0 and 450 give the same checksum. JSON tools (a browser's
    JSON.parse, for example) do not keep the difference, and SQLite stores both the same way."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _canonical(tables: Any) -> bytes:
    return json.dumps(_normalize(tables), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _checksum(tables: Any) -> str:
    return hashlib.sha256(_canonical(tables)).hexdigest()


def _signature(checksum: str, created_at: str) -> str:
    message = f"{FORMAT}:{VERSION}:{created_at}:{checksum}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$base64"} and isinstance(value["$base64"], str):
        return base64.b64decode(value["$base64"])
    return value


def _table_columns(connection: Any, table: str) -> list[dict[str, Any]]:
    return [dict(zip(row.keys(), tuple(row))) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def _is_local_meta(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(LOCAL_META_PREFIXES)


def history() -> list[dict[str, Any]]:
    try:
        value = json.loads(admin_repository.get_setting(HISTORY_KEY) or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _record(event: dict[str, Any]) -> None:
    """Add a backup/restore event to the history. Never raises: the history is informational."""
    try:
        items = [event] + history()
        admin_repository.set_setting(HISTORY_KEY, json.dumps(items[:HISTORY_LIMIT]))
    except Exception:  # pragma: no cover - logged, the backup itself already succeeded or failed
        logger.exception("Could not record the backup history event")


def status() -> dict[str, Any]:
    items = history()
    backups = [item for item in items if item.get("type") == "backup" and item.get("status") == "success"]
    restores = [item for item in items if item.get("type") == "restore" and item.get("status") == "success"]
    return {
        "database": "Turso (hosted SQLite)" if _database_kind() == "turso" else "SQLite database file",
        "tables": list(BACKUP_TABLES),
        "last_backup": backups[0] if backups else None,
        "last_restore": restores[0] if restores else None,
        "history": items,
        "max_upload_mb": MAX_BACKUP_BYTES // (1024 * 1024),
        # A temporary signing key changes on restart, which would make today's backups unrestorable.
        "signing_key_persistent": settings.secret_key_persistent,
    }


# ================================================================== backup


def create_backup(actor: str) -> tuple[bytes, str, dict[str, Any]]:
    """Read every backed-up table and return (file bytes, file name, history event)."""
    created_at = _now()
    try:
        tables: dict[str, Any] = {}
        with connection_scope() as connection:
            for table in BACKUP_TABLES:
                columns = [column["name"] for column in _table_columns(connection, table)]
                if not columns:
                    raise RuntimeError(f"The {table} table does not exist.")
                rows = connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall()
                values = [[_encode_value(value) for value in tuple(row)] for row in rows]
                if table == "app_meta":
                    values = [row for row in values if not _is_local_meta(row[columns.index("key")])]
                tables[table] = {"columns": columns, "rows": values}
    except (sqlite3.Error, RuntimeError) as error:
        logger.exception("Database backup failed")
        event = {"type": "backup", "status": "failed", "at": created_at, "by": actor, "error": "The database could not be read."}
        _record(event)
        raise HTTPException(status_code=500, detail="The backup could not be created because the database could not be read. Try again.") from error
    checksum = _checksum(tables)
    counts = {table: len(data["rows"]) for table, data in tables.items()}
    document = {
        "format": FORMAT,
        "version": VERSION,
        "application": "ImPRESSion PrintShop",
        "created_at": created_at,
        "created_by": actor,
        "database": _database_kind(),
        "counts": counts,
        "tables": tables,
        "checksum": checksum,
        "signature": _signature(checksum, created_at),
    }
    content = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stamp = created_at[:19].replace("-", "").replace(":", "").replace("T", "-")
    filename = f"impression-printshop-backup-{stamp}.json"
    event = {
        "type": "backup", "status": "success", "at": created_at, "by": actor, "file": filename,
        "size": len(content), "rows": sum(counts.values()), "checksum": checksum[:16],
    }
    _record(event)
    return content, filename, event


# ================================================================== restore


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"This backup file cannot be restored: {detail}")


def validate_backup(document: Any) -> dict[str, Any]:
    """Check a backup completely before anything is changed. Returns the table data to restore."""
    if not isinstance(document, dict):
        raise _invalid("it is not an ImPRESSion PrintShop backup.")
    if document.get("format") != FORMAT:
        raise _invalid("it is not an ImPRESSion PrintShop backup.")
    if document.get("version") != VERSION:
        raise _invalid(f"backup version {document.get('version')!r} is not supported.")
    tables = document.get("tables")
    created_at = document.get("created_at")
    if not isinstance(tables, dict) or not isinstance(created_at, str):
        raise _invalid("the file is incomplete.")
    required_tables = set(BACKUP_TABLES) - set(OPTIONAL_TABLES)
    if not required_tables <= set(tables) <= set(BACKUP_TABLES):
        missing = sorted(required_tables - set(tables))
        extra = sorted(set(tables) - set(BACKUP_TABLES))
        raise _invalid(f"unexpected tables (missing: {', '.join(missing) or 'none'}; unknown: {', '.join(extra) or 'none'}).")
    checksum = document.get("checksum")
    if not isinstance(checksum, str) or not hmac.compare_digest(_checksum(tables), checksum):
        raise _invalid("the data does not match its checksum, so the file is damaged or was edited.")
    signature = document.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(_signature(checksum, created_at), signature):
        raise _invalid(
            "it was not created by this shop's system (or the server's signing key has changed since). "
            "Only backups downloaded from this admin panel can be restored."
        )
    with connection_scope() as connection:
        schemas = {table: _table_columns(connection, table) for table in BACKUP_TABLES}
        missing_optional = [table for table in OPTIONAL_TABLES if table not in tables]
        if missing_optional:
            tables = dict(tables)
            categories = tables.get("catalog_categories") or {}
            category_ids: set[Any] = set()
            if isinstance(categories, dict) and isinstance(categories.get("columns"), list) and "id" in categories["columns"]:
                position = categories["columns"].index("id")
                category_ids = {row[position] for row in categories.get("rows", []) if isinstance(row, list) and len(row) > position}
            for table in missing_optional:
                columns = [column["name"] for column in schemas[table]]
                rows = [[_encode_value(value) for value in tuple(row)] for row in connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall()]
                if "category_id" in columns:
                    rows = [row for row in rows if row[columns.index("category_id")] in category_ids]
                tables[table] = {"columns": columns, "rows": rows}
    for table in BACKUP_TABLES:
        data = tables[table]
        if not isinstance(data, dict) or not isinstance(data.get("columns"), list) or not isinstance(data.get("rows"), list):
            raise _invalid(f"the {table} table is malformed.")
        columns = data["columns"]
        known = {column["name"]: column for column in schemas[table]}
        if not known:
            raise _invalid(f"the {table} table does not exist in this database.")
        if len(set(columns)) != len(columns) or not all(isinstance(name, str) and name in known for name in columns):
            raise _invalid(f"the {table} table has columns this system does not use.")
        required = [name for name, column in known.items() if column["notnull"] and column["dflt_value"] is None and not column["pk"]]
        missing = [name for name in required if name not in columns]
        if missing:
            raise _invalid(f"the {table} table is missing required columns: {', '.join(missing)}.")
        json_positions = [columns.index(name) for name in JSON_COLUMNS.get(table, ()) if name in columns]
        for number, row in enumerate(data["rows"], start=1):
            if not isinstance(row, list) or len(row) != len(columns):
                raise _invalid(f"row {number} of {table} is malformed.")
            for value in row:
                if not (value is None or isinstance(value, (str, int, float)) or (isinstance(value, dict) and set(value) == {"$base64"})):
                    raise _invalid(f"row {number} of {table} contains an unsupported value.")
            for position in json_positions:
                if row[position] is None:
                    continue
                try:
                    json.loads(row[position])
                except (TypeError, ValueError) as error:
                    raise _invalid(f"row {number} of {table} has damaged {columns[position]} data.") from error
    return tables


def _run_many(connection: Any, statements: list[tuple[str, list[Any]]]) -> None:
    batch = getattr(connection, "execute_batch", None)
    if batch is not None:
        batch(statements)  # Turso: many statements per HTTP request.
        return
    for sql, parameters in statements:
        connection.execute(sql, parameters)


def restore_backup(document: Any, confirmation: Any, actor: str) -> dict[str, Any]:
    """Replace the shop's data with a validated backup, in one transaction."""
    if confirmation != RESTORE_CONFIRMATION:
        raise HTTPException(status_code=422, detail=f'Type {RESTORE_CONFIRMATION} to confirm that the current data will be replaced.')
    started = _now()
    try:
        tables = validate_backup(document)
    except HTTPException as error:
        _record({"type": "restore", "status": "failed", "at": started, "by": actor, "error": str(error.detail)[:300]})
        raise
    statements: list[tuple[str, list[Any]]] = [("PRAGMA defer_foreign_keys = ON", [])]
    for table in reversed(BACKUP_TABLES):
        if table == "app_meta":
            statements.append(("DELETE FROM app_meta WHERE key NOT LIKE 'admin.%' AND key NOT LIKE 'backup.%'", []))
        else:
            statements.append((f"DELETE FROM {table}", []))
    counts: dict[str, int] = {}
    for table in BACKUP_TABLES:
        columns = tables[table]["columns"]
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        rows = tables[table]["rows"]
        if table == "app_meta":
            rows = [row for row in rows if not _is_local_meta(row[columns.index("key")])]
        statements.extend((sql, [_decode_value(value) for value in row]) for row in rows)
        counts[table] = len(rows)
    try:
        with connection_scope() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _run_many(connection, statements)
            # Every reference must point at a restored row before the transaction is committed.
            problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise sqlite3.IntegrityError(f"{len(problems)} records refer to missing records")
    except sqlite3.Error as error:
        logger.exception("Database restore failed and was rolled back")
        _record({"type": "restore", "status": "failed", "at": started, "by": actor, "error": "The data could not be written; nothing was changed."})
        raise HTTPException(
            status_code=422,
            detail=f"The backup could not be restored, so the current data was left unchanged. ({error})",
        ) from error
    event = {
        "type": "restore", "status": "success", "at": _now(), "by": actor, "rows": sum(counts.values()),
        "backup_created_at": document.get("created_at"), "backup_created_by": document.get("created_by", ""),
    }
    _record(event)
    return {"restored": counts, "event": event, "status": status()}
