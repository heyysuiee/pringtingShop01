from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Current UTC time as a second-precision ISO 8601 string, the format stored in the database."""
    return now_utc().isoformat(timespec="seconds")
