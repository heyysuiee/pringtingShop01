from __future__ import annotations

from datetime import datetime, timedelta, timezone


# Three-character shop prefix. Reference format: XXX-MM-DD-YY (e.g. IPS-11-04-26).
ORDER_REFERENCE_PREFIX = "IPS"

# The shop operates on Philippine time (fixed UTC+8, no daylight saving), so the date in a
# reference is the shop's calendar date even when the server runs in another time zone.
SHOP_TIMEZONE = timezone(timedelta(hours=8), "Asia/Manila")


def base_reference(created_at: datetime) -> str:
    """Return the XXX-MM-DD-YY reference for an order created at ``created_at``."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    local = created_at.astimezone(SHOP_TIMEZONE)
    return f"{ORDER_REFERENCE_PREFIX}-{local:%m-%d-%y}"


def candidate_reference(base: str, sequence: int) -> str:
    """The first order of a day uses the bare reference. Later orders on the same day get a numeric suffix."""
    return base if sequence <= 1 else f"{base}-{sequence}"
