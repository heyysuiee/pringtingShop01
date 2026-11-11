from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator

from backend.app.core.config import settings
from backend.app.database.turso import TursoConnection


DATABASE_PATH = settings.database_path


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
