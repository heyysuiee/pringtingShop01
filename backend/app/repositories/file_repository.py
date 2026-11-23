"""Uploaded file storage in the database: chunked upload sessions and stored order files.

Tables
------
upload_sessions  a file a signed-in customer is uploading in pieces before submitting an order (owner, product,
                 upload field, original name, type, declared size, bytes received, completed flag)
file_chunks      the bytes of an upload session or of a stored file, split into pieces of at most 2 MiB
                 (``owner`` is the session id while uploading, then the stored file name)
stored_files     files kept in the database (``PRINTSHOP_FILE_STORAGE=database``, the default with Turso)

Uploading in pieces keeps every request under the hosting request-size limit (Vercel: 4.5 MB), so large
Tarpaulin print files can be uploaded; storing the pieces in the database keeps them on hosts whose disk is
temporary and shares them between every copy of the app.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Any

from backend.app.database.connection import connection_scope, exclusive_write
from backend.app.utils.clock import now_iso


STORED_NAME = re.compile(r"[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|pdf)", re.IGNORECASE)


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS upload_sessions (
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        field_key TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        extension TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        received INTEGER NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_upload_sessions_customer ON upload_sessions(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_upload_sessions_created ON upload_sessions(created_at)",
    """
    CREATE TABLE IF NOT EXISTS file_chunks (
        owner TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        data BLOB NOT NULL,
        PRIMARY KEY (owner, chunk_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stored_files (
        filename TEXT PRIMARY KEY,
        mime_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        chunk_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


def initialize() -> None:
    with exclusive_write() as connection:
        for statement in SCHEMA:
            connection.execute(statement)


def _session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "product_id": row["product_id"],
        "field_key": row["field_key"],
        "name": row["name"],
        "extension": row["extension"],
        "mime_type": row["mime_type"],
        "size": int(row["size"]),
        "received": int(row["received"]),
        "chunk_count": int(row["chunk_count"]),
        "completed": bool(row["completed"]),
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------ upload sessions


def purge_sessions_before(cutoff: str) -> int:
    """Forget unfinished or unused uploads started before ``cutoff`` (ISO time) and their bytes."""
    with connection_scope() as connection:
        connection.execute(
            "DELETE FROM file_chunks WHERE owner IN (SELECT id FROM upload_sessions WHERE created_at < ?)", (cutoff,)
        )
        cursor = connection.execute("DELETE FROM upload_sessions WHERE created_at < ?", (cutoff,))
        return max(cursor.rowcount, 0)


def open_session_count(customer_id: str) -> int:
    with connection_scope() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM upload_sessions WHERE customer_id = ?", (customer_id,)).fetchone()[0])


def create_session(data: dict[str, Any]) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO upload_sessions (id, customer_id, product_id, field_key, name, extension, mime_type, size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (data["id"], data["customer_id"], data["product_id"], data["field_key"], data["name"], data["extension"],
             data["mime_type"], int(data["size"]), now_iso()),
        )


def get_session(session_id: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        return _session(connection.execute("SELECT * FROM upload_sessions WHERE id = ?", (session_id,)).fetchone())


def write_chunk(session_id: str, index: int, data: bytes, received_before: int, previous_size: int) -> None:
    """Store chunk ``index`` and update the counters, only if nobody else changed the session meanwhile.

    ``received_before`` and ``previous_size`` describe the session as the caller validated it; a retried last
    chunk replaces the stored one (``previous_size`` is its length), a new chunk is appended (``previous_size``
    is 0).
    """
    with connection_scope() as connection:
        updated = connection.execute(
            """
            UPDATE upload_sessions
            SET received = received - ? + ?, chunk_count = MAX(chunk_count, ?)
            WHERE id = ? AND received = ? AND completed = 0
            """,
            (previous_size, len(data), index + 1, session_id, received_before),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("The upload changed while this piece was being saved.")
        connection.execute(
            "INSERT OR REPLACE INTO file_chunks (owner, chunk_index, data) VALUES (?, ?, ?)",
            (session_id, index, data),
        )


def chunk_length(owner: str, index: int) -> int:
    with connection_scope() as connection:
        row = connection.execute("SELECT LENGTH(data) FROM file_chunks WHERE owner = ? AND chunk_index = ?", (owner, index)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def mark_completed(session_id: str) -> None:
    with connection_scope() as connection:
        connection.execute("UPDATE upload_sessions SET completed = 1 WHERE id = ?", (session_id,))


def delete_session(session_id: str, *, keep_chunks: bool = False) -> None:
    with connection_scope() as connection:
        if not keep_chunks:
            connection.execute("DELETE FROM file_chunks WHERE owner = ?", (session_id,))
        connection.execute("DELETE FROM upload_sessions WHERE id = ?", (session_id,))


# ------------------------------------------------------------------ chunk reads


def first_bytes(owner: str, length: int) -> bytes:
    with connection_scope() as connection:
        row = connection.execute(
            "SELECT substr(data, 1, ?) FROM file_chunks WHERE owner = ? AND chunk_index = 0", (length, owner)
        ).fetchone()
    value = row[0] if row else b""
    return bytes(value) if value is not None else b""


def chunk_indexes(owner: str) -> list[int]:
    with connection_scope() as connection:
        rows = connection.execute("SELECT chunk_index FROM file_chunks WHERE owner = ? ORDER BY chunk_index", (owner,)).fetchall()
    return [int(row[0]) for row in rows]


def read_chunk(owner: str, index: int) -> bytes:
    with connection_scope() as connection:
        row = connection.execute("SELECT data FROM file_chunks WHERE owner = ? AND chunk_index = ?", (owner, index)).fetchone()
    return bytes(row[0]) if row and row[0] is not None else b""


def iter_chunks(owner: str) -> Iterator[bytes]:
    """The file's pieces in order, one database read per piece (never the whole file at once)."""
    for index in chunk_indexes(owner):
        yield read_chunk(owner, index)


# ------------------------------------------------------------------ stored files


def store_chunks(filename: str, mime_type: str, chunks: list[bytes]) -> None:
    with connection_scope() as connection:
        for index, chunk in enumerate(chunks):
            connection.execute("INSERT INTO file_chunks (owner, chunk_index, data) VALUES (?, ?, ?)", (filename, index, chunk))
        connection.execute(
            "INSERT INTO stored_files (filename, mime_type, size, chunk_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (filename, mime_type, sum(len(chunk) for chunk in chunks), len(chunks), now_iso()),
        )


def adopt_session(session_id: str, filename: str, mime_type: str, size: int, chunk_count: int) -> None:
    """Turn a completed upload session's pieces into a stored file without copying the bytes."""
    with connection_scope() as connection:
        connection.execute("UPDATE file_chunks SET owner = ? WHERE owner = ?", (filename, session_id))
        connection.execute(
            "INSERT INTO stored_files (filename, mime_type, size, chunk_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (filename, mime_type, size, chunk_count, now_iso()),
        )
        connection.execute("DELETE FROM upload_sessions WHERE id = ?", (session_id,))


def stored_file(filename: str) -> dict[str, Any] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM stored_files WHERE filename = ?", (filename,)).fetchone()
    if row is None:
        return None
    return {"filename": row["filename"], "mime_type": row["mime_type"], "size": int(row["size"]), "chunk_count": int(row["chunk_count"])}


def delete_stored_file(filename: str) -> None:
    with connection_scope() as connection:
        connection.execute("DELETE FROM file_chunks WHERE owner = ?", (filename,))
        connection.execute("DELETE FROM stored_files WHERE filename = ?", (filename,))


# ------------------------------------------------------------------ orphan cleanup


def delete_orphan_chunks() -> int:
    """Remove byte chunks whose upload session or stored file no longer exists."""
    with connection_scope() as connection:
        cursor = connection.execute(
            """
            DELETE FROM file_chunks
            WHERE owner NOT IN (SELECT id FROM upload_sessions)
              AND owner NOT IN (SELECT filename FROM stored_files)
            """
        )
        return max(cursor.rowcount, 0)


def delete_empty_stored_files() -> int:
    """Remove stored-file records with no byte chunks (a file that was never fully written)."""
    with connection_scope() as connection:
        cursor = connection.execute(
            "DELETE FROM stored_files WHERE NOT EXISTS (SELECT 1 FROM file_chunks WHERE owner = stored_files.filename)"
        )
        return max(cursor.rowcount, 0)


def referenced_stored_names() -> set[str]:
    """Every generated file name mentioned anywhere in the database (JSON columns included)."""
    names: set[str] = set()
    with connection_scope() as connection:
        tables = [str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            if table == "file_chunks":  # chunk bytes are not references, and can be very large
                continue
            columns = [str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            selected = ", ".join(f'"{column}"' for column in columns)
            for row in connection.execute(f"SELECT {selected} FROM \"{table}\""):
                for value in tuple(row):
                    if isinstance(value, str):
                        names.update(match.group(0).lower() for match in STORED_NAME.finditer(value))
    return names
