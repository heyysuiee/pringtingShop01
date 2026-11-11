from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class OrderItem(TypedDict):
    id: NotRequired[str]
    category_id: NotRequired[str]
    name: str
    category: str
    description: str
    image: str


class OrderSpecification(TypedDict):
    key: str
    label: str
    value: str
    type: str


class OrderCustomization(TypedDict):
    """Product configuration saved with an order.

    Orders placed through the catalog form store ``options`` (validated raw values keyed by field),
    ``specifications`` (the readable label/value list shown to staff and customers) and ``notes``.
    Orders placed before the catalog form existed store the older ``size``/``material``/``details``/
    ``instructions`` keys instead; both shapes are displayed.
    """

    quantity: int
    options: NotRequired[dict[str, Any]]
    specifications: NotRequired[list[OrderSpecification]]
    notes: NotRequired[str]
    # "package" when a ready-made invitation set was chosen, "customize" for a built-from-scratch order.
    order_kind: NotRequired[str]
    # Snapshot of the chosen invitation package at submission time (name, price, inclusions, ...).
    package: NotRequired[dict[str, Any]]
    size: NotRequired[str]
    material: NotRequired[str]
    details: NotRequired[str]
    instructions: NotRequired[str]
