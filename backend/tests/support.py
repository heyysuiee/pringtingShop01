"""Small helpers shared by the test modules.

Import this only after the test module has set its PRINTSHOP_* environment variables, like the
application imports (it loads the order repository, which reads the settings).
"""
from __future__ import annotations

from typing import TypeVar

from backend.app.models.order import OrderRecord
from backend.app.repositories import order_repository


T = TypeVar("T")


def present(value: T | None, what: str = "value") -> T:
    """``value``, failing the test clearly when it is None (also tells the type checker it is not None)."""
    if value is None:
        raise AssertionError(f"Expected a {what}, got None.")
    return value


def stored_order(reference: str) -> OrderRecord:
    """The saved order with this reference; the test fails if it does not exist."""
    return present(order_repository.get_order(reference), f"saved order {reference}")
