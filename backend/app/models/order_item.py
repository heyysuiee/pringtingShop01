from __future__ import annotations

from typing import TypedDict


class OrderItem(TypedDict):
    name: str
    category: str
    description: str
    image: str


class OrderCustomization(TypedDict):
    size: str
    quantity: int
    material: str
    details: str
    instructions: str
