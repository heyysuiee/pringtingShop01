from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator

from backend.app.core.config import settings


DATABASE_PATH = settings.database_path


def connect() -> sqlite3.Connection:
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