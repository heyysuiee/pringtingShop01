"""Connection to a Turso (libSQL) database over its HTTP API.

Vercel runs several short-lived copies of the app, so a local SQLite file is neither kept nor
shared between them. Turso is SQLite hosted online: the SQL stays exactly the same, only the
connection changes. This module implements the part of the sqlite3 interface the repositories
use (execute, fetchone/fetchall, rowcount, rows read by name or index, `with connection:`
transactions, and sqlite3.IntegrityError for constraint failures), using only the standard
library and Turso's "Hrana over HTTP" protocol (POST /v2/pipeline).

All statements of one connection run in one server-side stream (kept with the "baton"), so
BEGIN ... COMMIT transactions work as they do with a local file.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
import urllib.request
from typing import Any, Iterator, Mapping, Sequence


# Python's sqlite3 module (legacy transaction control, the default) opens a transaction
# implicitly before these statements; the same is done here so `with connection:` behaves alike.
_IMPLICIT_BEGIN = {"INSERT", "UPDATE", "DELETE", "REPLACE"}


def _http_url(url: str) -> str:
    url = url.strip().rstrip("/")
    for scheme in ("libsql://", "wss://", "ws://"):
        if url.startswith(scheme):
            secure = scheme != "ws://"
            return ("https://" if secure else "http://") + url[len(scheme):]
    return url


def _first_keyword(sql: str) -> str:
    words = sql.lstrip().split(None, 1)
    return words[0].upper().rstrip(";") if words else ""


def _encode(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii").rstrip("=")}
    raise sqlite3.InterfaceError(f"Unsupported parameter type: {type(value).__name__}")


def _decode(value: Mapping[str, Any]) -> Any:
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "text":
        return value["value"]
    if kind == "blob":
        encoded = value.get("base64", "")
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    raise sqlite3.InterfaceError(f"Unsupported value type from Turso: {kind}")


def _raise_error(error: Mapping[str, Any]) -> None:
    message = str(error.get("message", "Turso statement failed"))
    code = str(error.get("code") or "")
    if "CONSTRAINT" in code.upper() or "constraint failed" in message.lower():
        raise sqlite3.IntegrityError(message)
    raise sqlite3.OperationalError(message)


class Row:
    """Behaves like sqlite3.Row: row["name"] (case-insensitive), row[0], row.keys(), dict(row)."""

    __slots__ = ("_columns", "_index", "_values")

    def __init__(self, columns: tuple[str, ...], index: dict[str, int], values: tuple[Any, ...]) -> None:
        self._columns = columns
        self._index = index
        self._values = values

    def keys(self) -> list[str]:
        return list(self._columns)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        position = self._index.get(key)
        if position is None:
            position = self._index.get(key.lower())
        if position is None:
            raise IndexError("No item with that key")
        return self._values[position]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._columns, self._values))!r})"


class Cursor:
    def __init__(self, rows: list[Row], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self._position = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Row | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Row]:
        while (row := self.fetchone()) is not None:
            yield row


class TursoConnection:
    def __init__(self, url: str, auth_token: str, timeout: float = 15) -> None:
        self._base_url = _http_url(url)
        self._auth_token = auth_token
        self._timeout = timeout
        self._baton: str | None = None
        # Sent together with the first statement, so it costs no extra round trip.
        self._setup: list[str] = ["PRAGMA foreign_keys = ON"]
        self.in_transaction = False

    # -- transport -------------------------------------------------------------------

    def _pipeline(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        body = json.dumps({"baton": self._baton, "requests": requests}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        request = urllib.request.Request(self._base_url + "/v2/pipeline", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise sqlite3.OperationalError(f"Turso request failed ({error.code}): {detail}") from error
        except (urllib.error.URLError, OSError) as error:
            raise sqlite3.OperationalError(f"Turso request failed: {error}") from error
        self._baton = payload.get("baton")
        if payload.get("base_url"):
            self._base_url = _http_url(payload["base_url"])
        return payload.get("results", [])

    @staticmethod
    def _statement(sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> dict[str, Any]:
        statement: dict[str, Any] = {"sql": sql, "want_rows": True}
        if isinstance(parameters, Mapping):
            statement["named_args"] = [{"name": f":{name}", "value": _encode(value)} for name, value in parameters.items()]
        elif parameters:
            statement["args"] = [_encode(value) for value in parameters]
        return statement

    # -- sqlite3-compatible interface ------------------------------------------------

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        keyword = _first_keyword(sql)
        prefix = [{"type": "execute", "stmt": {"sql": statement}} for statement in self._setup]
        self._setup = []
        implicit_begin = not self.in_transaction and keyword in _IMPLICIT_BEGIN
        if implicit_begin:
            prefix.append({"type": "execute", "stmt": {"sql": "BEGIN"}})
        results = self._pipeline(prefix + [{"type": "execute", "stmt": self._statement(sql, parameters)}])
        for result in results[: len(prefix)]:
            if result.get("type") != "ok":
                _raise_error(result.get("error", {}))
        if implicit_begin:
            self.in_transaction = True
        outcome = results[-1] if results else {"type": "error", "error": {"message": "Empty response from Turso"}}
        if outcome.get("type") != "ok":
            _raise_error(outcome.get("error", {}))
        if keyword == "BEGIN":
            self.in_transaction = True
        elif keyword in {"COMMIT", "END"} or (keyword == "ROLLBACK" and " TO " not in f" {sql.upper()} "):
            self.in_transaction = False
        result = outcome["response"]["result"]
        columns = tuple(str(column.get("name") or "") for column in result.get("cols", []))
        index: dict[str, int] = {}
        for position, name in enumerate(columns):
            index.setdefault(name, position)
            index.setdefault(name.lower(), position)
        rows = [Row(columns, index, tuple(_decode(value) for value in values)) for values in result.get("rows", [])]
        rowcount = int(result.get("affected_row_count", 0)) if keyword in _IMPLICIT_BEGIN else -1
        last_id = result.get("last_insert_rowid")
        return Cursor(rows, rowcount, int(last_id) if last_id is not None else None)

    def commit(self) -> None:
        if self.in_transaction:
            self.execute("COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            self.execute("ROLLBACK")

    def close(self) -> None:
        if self._baton is None:
            return
        try:
            # Closing the stream also discards a transaction that was never committed.
            self._pipeline([{"type": "close"}])
        except sqlite3.Error:
            pass
        finally:
            self._baton = None
            self.in_transaction = False

    def __enter__(self) -> "TursoConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is None:
            self.commit()
        else:
            try:
                self.rollback()
            except sqlite3.Error:
                pass
        return False
