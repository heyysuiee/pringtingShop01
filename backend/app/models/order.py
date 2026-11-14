from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from backend.app.models.order_item import OrderCustomization, OrderItem


class OrderAttachment(TypedDict):
    id: str
    name: str
    filename: str
    mime_type: str
    size: int
    # Set when the file was uploaded through a product-specific upload field (e.g. "Photo upload").
    field_key: NotRequired[str]
    field_label: NotRequired[str]


class OrderHistoryEntry(TypedDict):
    at: str
    text: str


class OrderUpdates(TypedDict):
    """Fields that may change after an order is created.

    order_repository._apply_updates copies exactly these fields; add a new field there as well.
    """

    status: NotRequired[str]
    total_amount: NotRequired[float | None]
    payment_instructions: NotRequired[str]
    payments: NotRequired[list[dict[str, Any]]]
    attachments: NotRequired[list[OrderAttachment]]
    draft_attachment: NotRequired[OrderAttachment | None]
    admin_notes: NotRequired[str]
    payment_option: NotRequired[str]
    quotation: NotRequired[dict[str, Any]]
    payment_submissions: NotRequired[list[dict[str, Any]]]


# How a customer pays for an order. An empty payment option means "not chosen yet": customized
# orders choose it only after accepting the administrator's quotation.
PAYMENT_OPTIONS = {"down_payment": "50% Down Payment", "cod": "Cash on Delivery (COD)"}
PAYMENT_OPTION_PENDING_LABEL = "Chosen after the quotation is accepted"
DOWN_PAYMENT_RATE = 0.5

# How the finished order reaches the customer. Older orders have no fulfillment and were picked up.
FULFILLMENT_METHODS = {"pickup": "Store Pickup", "delivery": "Delivery"}
# Delivery address fields, in display order: (key, label, required, max length).
DELIVERY_FIELDS = (
    ("recipient_name", "Customer name", True, 120),
    ("contact_number", "Contact number", True, 40),
    ("street", "House number / Street", True, 200),
    ("barangay", "Barangay", True, 120),
    ("city", "City / Municipality", True, 120),
    ("province", "Province", True, 120),
    ("landmark", "Landmark", False, 200),
)


class OrderRecord(TypedDict):
    reference: str
    created_at: str
    updated_at: str
    customer: dict[str, str]
    product: OrderItem
    customization: OrderCustomization
    # Invitation orders store an ordered list of {label, value}; older orders store the five fixed keys.
    event_details: dict[str, str] | list[dict[str, str]]
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
    # Estimate from administrator-configured catalog prices. The confirmed price is total_amount.
    pricing: NotRequired[dict[str, Any]]
    # Set when the order moved to Completed (UTC ISO 8601). Never entered by hand.
    completed_at: NotRequired[str | None]
    # "down_payment", "cod" (see PAYMENT_OPTIONS), or "" while a customized order waits for its quotation.
    payment_option: NotRequired[str]
    # {"method": "pickup" | "delivery", plus the DELIVERY_FIELDS for deliveries}. Independent of payment.
    fulfillment: NotRequired[dict[str, Any]]
    # The administrator's quotation and the customer's response:
    # {"amount", "cod_allowed", "revision", "sent_at", "status": "sent" | "accepted" | "declined",
    #  "responded_at", "decline_reason", "note", "cod_confirmed_at"}.
    quotation: NotRequired[dict[str, Any]]
    # Payments reported by the customer (reference + proof), waiting for the administrator to verify:
    # {"id", "amount", "method", "reference", "proof", "submitted_at", "status": "pending" | "verified" | "rejected",
    #  "reviewed_at", "review_note"}. A verified submission becomes exactly one entry in payments.
    payment_submissions: NotRequired[list[dict[str, Any]]]
