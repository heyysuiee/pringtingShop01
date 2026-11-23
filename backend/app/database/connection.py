from __future__ import annotations

import sqlite3
import contextvars
from contextlib import contextmanager
from typing import Generator

from backend.app.core.config import settings
from backend.app.database.turso import TursoConnection


DATABASE_PATH = settings.database_path


_exclusive_connection: contextvars.ContextVar[sqlite3.Connection | None] = contextvars.ContextVar(
    "printshop_exclusive_connection", default=None
)


def connect() -> sqlite3.Connection:
    # On Vercel the data lives in Turso (shared by every copy of the app); locally and in the
    # tests it is a SQLite file. Both understand the same SQL.
    if settings.turso_database_url:
        return TursoConnection(settings.turso_database_url, settings.turso_auth_token)  # type: ignore[return-value]
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


@contextmanager
def connection_scope() -> Generator[sqlite3.Connection, None, None]:
    connection = connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@contextmanager
def exclusive_write() -> Generator[sqlite3.Connection, None, None]:
    """One process-wide write transaction for schema creation and seed data.

    Startup can run in more than one worker (and a worker can call another repository's ``initialize()``).
    Taking the SQLite write lock before checking seed markers means exactly one worker seeds the database;
    the others see the committed marker and do nothing. Nested calls reuse the caller's transaction.
    """
    current = _exclusive_connection.get()
    if current is not None:
        yield current
        return
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        token = _exclusive_connection.set(connection)
        try:
            yield connection
        finally:
            _exclusive_connection.reset(token)
