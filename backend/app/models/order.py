from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from backend.app.models.order_item import OrderCustomization, OrderItem


class OrderAttachment(TypedDict):
    id: str
    name: str
    filename: str
    mime_type: str
    size: int


class OrderHistoryEntry(TypedDict):
    at: str
    text: str


class OrderUpdates(TypedDict):
    status: NotRequired[str]
    total_amount: NotRequired[float | None]
    payment_instructions: NotRequired[str]
    payments: NotRequired[list[dict[str, Any]]]
    attachments: NotRequired[list[OrderAttachment]]
    draft_attachment: NotRequired[OrderAttachment | None]
    admin_notes: NotRequired[str]


class OrderRecord(TypedDict):
    reference: str
    created_at: str
    updated_at: str
    customer: dict[str, str]
    product: OrderItem
    customization: OrderCustomization
    event_details: dict[str, str]
    has_event: bool
    acknowledged: bool
    status: str
    total_amount: float | None
    payment_instructions: str
    payments: list[dict[str, Any]]
    attachments: list[OrderAttachment]
    draft_attachment: OrderAttachment | None
    history: list[OrderHistoryEntry]
    admin_notes: str
    customer_id: NotRequired[str | None]
    idempotency_key: NotRequired[str | None]
