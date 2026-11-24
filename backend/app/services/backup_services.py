"""Database backup and restore for administrators.

Works the same for the local SQLite file and for Turso (hosted SQLite on Vercel): a backup is a JSON
document with the rows of every application table, read through the normal database connection, so no
database file path or credential is ever exposed or needed.

* **Backup** contains the shop's data: customers, the catalog and packages, orders (with their payments,
  submissions, quotations, and history), the audit log, customer notifications, app settings, and the
  database-backed uploads (``stored_files`` / ``file_chunks``). Files kept on disk are added as base64
  entries in the same document. It leaves out administrator credentials, password-reset codes, and
  sign-in throttling records, so a restore can never change the administrator's password or lock anyone out.
* Every table is read from one database snapshot, so an order cannot be backed up against a payment or
  audit row from a different moment.
* **Restore never erases the audit trail.** The audit log is append-only: a restore keeps every current
  entry, merges in the backup's entries (an entry already present is not duplicated), and records the
  restore itself as an audit entry. Rolling the data back can therefore never hide what happened after
  the backup was made.
* Each file carries a SHA-256 checksum of its data and an HMAC signature made with
  ``PRINTSHOP_BACKUP_SIGNING_KEY`` (or ``PRINTSHOP_SECRET_KEY`` when no dedicated key is set). A restore
  accepts only files signed by this installation and unchanged since they were created. Retired keys can
  be listed in ``PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS`` while old backups are still needed.
* **Restore** validates the whole file first (format, tables, columns, value types, JSON fields, file
  paths), stages disk files, then replaces the tables inside one transaction: either every table is
  restored or nothing changes. If the database write fails, staged disk changes are rolled back too.
* Backup and restore events (time, administrator, result, row counts) are kept in ``app_meta``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.database.connection import connection_scope
from backend.app.repositories import admin_repository, audit_repository


logger = logging.getLogger("printshop.backup")

FORMAT = "impression-printshop-backup"
VERSION = 2
# Version 1 backups did not contain upload bytes; they are still accepted so an upgrade never strands an
# older backup file. Their signature/checksum are computed with their own version number.
SUPPORTED_VERSIONS = (1, 2)
# Restored in this order (parents before the tables that reference them); cleared in reverse, except the
# append-only audit log, which is merged and never cleared.
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
    # Uploaded order files kept in the database. Their bytes are base64-encoded by _encode_value.
    "stored_files",
    "file_chunks",
    "orders",
    "order_audit_log",
    "customer_notifications",
)
# Tables added after backups were first made. A backup without them is still restored; this installation's
# rows of such a table are kept (those that still belong to a restored category).
OPTIONAL_TABLES = ("standard_sizes", "payment_methods", "stored_files", "file_chunks")
FILE_TABLES = ("stored_files", "file_chunks")
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
# Backups now carry upload files too, so the local/API limit is larger. Vercel's request-body limit still
# applies to restores there; the Settings page reports this limit to the administrator.
MAX_BACKUP_BYTES = 100 * 1024 * 1024
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


def _checksum_payload(tables: Any, files: dict[str, str] | None, version: int) -> Any:
    # Version 1 checksummed tables only. Keeping that shape means old files remain verifiable.
    return tables if version < 2 else {"tables": tables, "files": files or {}}


def _checksum(tables: Any, files: dict[str, str] | None = None, version: int = VERSION) -> str:
    return hashlib.sha256(_canonical(_checksum_payload(tables, files, version))).hexdigest()


def _signing_keys() -> tuple[str, ...]:
    keys = [settings.backup_signing_key or settings.secret_key]
    keys.extend(settings.previous_backup_signing_keys)
    return tuple(keys)


def _signature(checksum: str, created_at: str, version: int = VERSION) -> str:
    message = f"{FORMAT}:{version}:{created_at}:{checksum}".encode("utf-8")
    return hmac.new(_signing_keys()[0].encode("utf-8"), message, hashlib.sha256).hexdigest()


def _signature_matches(checksum: str, created_at: str, version: int, supplied: str) -> bool:
    message = f"{FORMAT}:{version}:{created_at}:{checksum}".encode("utf-8")
    return any(
        hmac.compare_digest(hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest(), supplied)
        for key in _signing_keys()
    )


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


def _upload_dir() -> Path:
    return Path(settings.upload_dir)


def _safe_file_path(value: Any) -> str | None:
    """A relative POSIX path under the upload directory, or None for an unsafe key."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) > 3 or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return value


def _read_disk_files() -> dict[str, str]:
    """Every file currently in PRINTSHOP_UPLOAD_DIR, keyed by its relative path and base64-encoded."""
    files: dict[str, str] = {}
    directory = _upload_dir()
    if not directory.is_dir():
        return files
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        key = path.relative_to(directory).as_posix()
        if _safe_file_path(key) is None:
            logger.warning("Skipping unsafe upload path in backup: %s", key)
            continue
        files[key] = base64.b64encode(path.read_bytes()).decode("ascii")
    return files


def _disk_file_count() -> int:
    directory = _upload_dir()
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.rglob("*") if path.is_file() and not path.is_symlink() and not path.name.startswith("."))


def _database_file_count() -> int:
    try:
        with connection_scope() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stored_files'").fetchone():
                return 0
            return int(connection.execute("SELECT COUNT(*) FROM stored_files").fetchone()[0])
    except sqlite3.Error:  # pragma: no cover - status is informational
        return 0


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
        "version": VERSION,
        "tables": list(BACKUP_TABLES),
        "file_tables": list(FILE_TABLES),
        "disk_files": _disk_file_count(),
        "database_files": _database_file_count(),
        "last_backup": backups[0] if backups else None,
        "last_restore": restores[0] if restores else None,
        "history": items,
        "max_upload_mb": MAX_BACKUP_BYTES // (1024 * 1024),
        # A temporary signing key changes on restart, which would make today's backups unrestorable.
        "signing_key_persistent": settings.backup_signing_key_persistent,
        "signing_key_source": "PRINTSHOP_BACKUP_SIGNING_KEY" if os.environ.get("PRINTSHOP_BACKUP_SIGNING_KEY", "").strip() else "PRINTSHOP_SECRET_KEY",
    }


# ================================================================== backup


def create_backup(actor: str) -> tuple[bytes, str, dict[str, Any]]:
    """Read one consistent database snapshot plus the upload files, and return the backup document."""
    created_at = _now()
    try:
        tables: dict[str, Any] = {}
        files: dict[str, str] = {}
        with connection_scope() as connection:
            # One write lock covers both the disk scan and every table; an order cannot commit a newly
            # saved file reference between the file scan and the SQL snapshot. Disk writes use an atomic
            # rename, so the scan also never reads a half-written file.
            connection.execute("BEGIN IMMEDIATE")
            files = _read_disk_files()
            for table in BACKUP_TABLES:
                columns = [column["name"] for column in _table_columns(connection, table)]
                if not columns:
                    raise RuntimeError(f"The {table} table does not exist.")
                rows = connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid").fetchall()
                values = [[_encode_value(value) for value in tuple(row)] for row in rows]
                if table == "app_meta":
                    values = [row for row in values if not _is_local_meta(row[columns.index("key")])]
                tables[table] = {"columns": columns, "rows": values}
    except (OSError, sqlite3.Error, RuntimeError) as error:
        logger.exception("Database backup failed")
        event = {"type": "backup", "status": "failed", "at": created_at, "by": actor, "error": "The shop data could not be read."}
        _record(event)
        raise HTTPException(status_code=500, detail="The backup could not be created because the shop data could not be read. Try again.") from error
    checksum = _checksum(tables, files, VERSION)
    counts = {table: len(data["rows"]) for table, data in tables.items()}
    document = {
        "format": FORMAT,
        "version": VERSION,
        "application": "ImPRESS Print Works",
        "created_at": created_at,
        "created_by": actor,
        "database": _database_kind(),
        "counts": counts,
        "tables": tables,
        "files": files,
        "file_count": len(files),
        "checksum": checksum,
        "signature": _signature(checksum, created_at, VERSION),
    }
    content = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stamp = created_at[:19].replace("-", "").replace(":", "").replace("T", "-")
    filename = f"impression-printshop-backup-{stamp}.json"
    event = {
        "type": "backup", "status": "success", "at": created_at, "by": actor, "file": filename,
        "size": len(content), "rows": sum(counts.values()), "files": len(files), "checksum": checksum[:16],
    }
    _record(event)
    return content, filename, event


# ================================================================== restore


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"This backup file cannot be restored: {detail}")


def validate_backup(document: Any) -> tuple[dict[str, Any], dict[str, bytes], int]:
    """Check a backup completely before anything is changed.

    Returns ``(tables, disk_files, version)``. Disk files are decoded here so no unsafe path or damaged
    base64 can reach the restore step.
    """
    if not isinstance(document, dict):
        raise _invalid("it is not an ImPRESS Print Works backup.")
    if document.get("format") != FORMAT:
        raise _invalid("it is not an ImPRESS Print Works backup.")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
        raise _invalid(f"backup version {document.get('version')!r} is not supported.")
    tables = document.get("tables")
    created_at = document.get("created_at")
    if not isinstance(tables, dict) or not isinstance(created_at, str):
        raise _invalid("the file is incomplete.")
    raw_files = document.get("files", {})
    if version >= 2 and not isinstance(raw_files, dict):
        raise _invalid("the attached upload files are malformed.")
    files: dict[str, bytes] = {}
    for key, value in raw_files.items() if isinstance(raw_files, dict) else []:
        safe_key = _safe_file_path(key)
        if safe_key is None:
            raise _invalid(f"the attached file path {key!r} is unsafe.")
        if not isinstance(value, str):
            raise _invalid(f"the attached file {safe_key} is malformed.")
        try:
            files[safe_key] = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise _invalid(f"the attached file {safe_key} is damaged.") from error
    required_tables = set(BACKUP_TABLES) - set(OPTIONAL_TABLES)
    if not required_tables <= set(tables) <= set(BACKUP_TABLES):
        missing = sorted(required_tables - set(tables))
        extra = sorted(set(tables) - set(BACKUP_TABLES))
        raise _invalid(f"unexpected tables (missing: {', '.join(missing) or 'none'}; unknown: {', '.join(extra) or 'none'}).")
    checksum = document.get("checksum")
    if not isinstance(checksum, str) or not hmac.compare_digest(_checksum(tables, raw_files if isinstance(raw_files, dict) else {}, version), checksum):
        raise _invalid("the data does not match its checksum, so the file is damaged or was edited.")
    signature = document.get("signature")
    if not isinstance(signature, str) or not _signature_matches(checksum, created_at, version, signature):
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
        if table == "order_audit_log" and "id" not in columns:
            raise _invalid("the order_audit_log table is missing its id column.")
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
    return tables, files, version


def _run_many(connection: Any, statements: list[tuple[str, list[Any]]]) -> None:
    batch = getattr(connection, "execute_batch", None)
    if batch is not None:
        batch(statements)  # Turso: many statements per HTTP request.
        return
    for sql, parameters in statements:
        connection.execute(sql, parameters)


class _DiskFileChanges:
    """Apply a backup's disk files with a rollback path if the database restore fails.

    Disk files cannot share a transaction with SQLite/Turso, so they are staged first, swapped into the
    upload directory second, and only kept when the database transaction commits. A failure at any
    point restores the previous files exactly.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        directory = _upload_dir()
        directory.parent.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix=".impression-backup-", dir=str(directory.parent))
        self._root = Path(self._temporary.name)
        self._applied: list[Path] = []
        self._undo: list[tuple[Path, Path]] = []
        self._finished = False

    def _backup_path(self, key: str, suffix: str = "") -> Path:
        target = self._root / "removed" / f"{key}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def apply(self) -> None:
        directory = _upload_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for key, contents in self.files.items():
            staged = self._root / "new" / key
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(contents)
        # Remove files that are not in the backup so the restored upload directory matches the file.
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                    continue
                key = path.relative_to(directory).as_posix()
                if key in self.files:
                    continue
                saved = self._backup_path(key, ".removed")
                shutil.move(str(path), str(saved))
                self._undo.append((path, saved))
        for key in self.files:
            target = directory / key
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                saved = self._backup_path(key, ".previous")
                shutil.move(str(target), str(saved))
                self._undo.append((target, saved))
            staged = self._root / "new" / key
            shutil.move(str(staged), str(target))
            self._applied.append(target)

    def rollback(self) -> None:
        for path in reversed(self._applied):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort rollback
                logger.exception("Could not remove restored file during rollback: %s", path)
        for target, saved in reversed(self._undo):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(saved), str(target))
            except OSError:  # pragma: no cover - best-effort rollback
                logger.exception("Could not restore previous file during rollback: %s", target)
        self.finish()

    def finish(self) -> None:
        if not self._finished:
            self._temporary.cleanup()
            self._finished = True


def restore_backup(document: Any, confirmation: Any, actor: str) -> dict[str, Any]:
    """Replace the shop's data with a validated backup, in one transaction.

    The audit trail is the exception: it is append-only, so a restore never deletes an audit entry. The
    backup's entries are merged in (an id already present is kept as it is), entries about orders this
    restore removes still survive, and the restore itself is recorded in the same transaction.
    """
    if confirmation != RESTORE_CONFIRMATION:
        raise HTTPException(status_code=422, detail=f'Type {RESTORE_CONFIRMATION} to confirm that the current data will be replaced.')
    started = _now()
    try:
        tables, files, version = validate_backup(document)
    except HTTPException as error:
        _record({"type": "restore", "status": "failed", "at": started, "by": actor, "error": str(error.detail)[:300]})
        raise
    statements: list[tuple[str, list[Any]]] = [("PRAGMA defer_foreign_keys = ON", [])]
    for table in reversed(BACKUP_TABLES):
        if table == "order_audit_log":
            continue  # Append-only: merged inside the transaction below, never cleared.
        if table == "app_meta":
            statements.append(("DELETE FROM app_meta WHERE key NOT LIKE 'admin.%' AND key NOT LIKE 'backup.%'", []))
        else:
            statements.append((f"DELETE FROM {table}", []))
    counts: dict[str, int] = {}
    for table in BACKUP_TABLES:
        if table == "order_audit_log":
            continue  # Inserted inside the transaction, after deduplicating against the entries kept.
        columns = tables[table]["columns"]
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        rows = tables[table]["rows"]
        if table == "app_meta":
            rows = [row for row in rows if not _is_local_meta(row[columns.index("key")])]
        statements.extend((sql, [_decode_value(value) for value in row]) for row in rows)
        counts[table] = len(rows)
    disk: _DiskFileChanges | None = None
    try:
        # Version 1 files have no disk-file section; leave the current upload directory untouched.
        if version >= 2:
            disk = _DiskFileChanges(files)
            disk.apply()
        with connection_scope() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Merge the backup's audit entries into the trail that is kept. Reading the existing ids inside
            # the write transaction means a concurrent action's entry can never be lost or duplicated.
            audit = tables["order_audit_log"]
            audit_columns = audit["columns"]
            id_position = audit_columns.index("id")
            existing = {row[0] for row in connection.execute("SELECT id FROM order_audit_log").fetchall()}
            audit_rows = [row for row in audit["rows"] if row[id_position] not in existing]
            audit_sql = (
                f"INSERT INTO order_audit_log ({', '.join(audit_columns)}) "
                f"VALUES ({', '.join('?' for _ in audit_columns)})"
            )
            statements.extend((audit_sql, [_decode_value(value) for value in row]) for row in audit_rows)
            counts["order_audit_log"] = len(audit_rows)
            _run_many(connection, statements)
            # Every reference must point at a restored row before the transaction is committed.
            problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise sqlite3.IntegrityError(f"{len(problems)} records refer to missing records")
            # The restore itself belongs to the trail. "*" marks a shop-wide entry (no single order); it is
            # written in the same transaction, so a failed restore leaves no entry behind.
            audit_repository.insert(connection, "*", _now(), {
                "actor_type": "admin",
                "actor_id": actor,
                "action": "backup.restored",
                "details": {
                    "backup_created_at": document.get("created_at"),
                    "backup_created_by": document.get("created_by", ""),
                    "backup_version": version,
                    "rows": dict(counts),
                    "files": len(files) if version >= 2 else 0,
                },
            })
    except (sqlite3.Error, OSError) as error:
        if disk is not None:
            disk.rollback()
        logger.exception("Restore failed and was rolled back")
        _record({"type": "restore", "status": "failed", "at": started, "by": actor, "error": "The data could not be written; nothing was changed."})
        raise HTTPException(
            status_code=422,
            detail=f"The backup could not be restored, so the current data was left unchanged. ({error})",
        ) from error
    if disk is not None:
        disk.finish()
    event = {
        "type": "restore", "status": "success", "at": _now(), "by": actor, "rows": sum(counts.values()),
        "files": len(files) if version >= 2 else 0,
        "backup_created_at": document.get("created_at"), "backup_created_by": document.get("created_by", ""),
    }
    _record(event)
    return {"restored": counts, "files_restored": event["files"], "event": event, "status": status()}
