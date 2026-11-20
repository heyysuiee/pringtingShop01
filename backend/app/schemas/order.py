from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OrderSubmission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    customer: Any = None
    product: Any = None
    customization: Any = None
    event: Any = None
    has_event: Any = None
    acknowledged: Any = None
    files: Any = None
    # Files attached to product-specific upload fields, keyed by field key: {"photo_upload": [{name, dataUrl}]}.
    field_files: Any = None
    # Optional; every order requires the 50% down payment ("down_payment"). "cod" is refused.
    payment_option: Any = None
    # {"method": "pickup"} or {"method": "delivery", "recipient_name", "contact_number", "street", "barangay",
    #  "city", "province", "landmark"}.
    fulfillment: Any = None


class AdminPaymentOption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment_option: Any = None
    note: Any = ""
    expected_version: Any = None


class CustomerOrderAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: Any = None


class CustomerOrderChanges(CustomerOrderAction):
    note: Any = ""


class CustomerQuotationResponse(CustomerOrderAction):
    # The quotation revision the customer saw, so a revised quotation is never accepted unseen.
    quotation_revision: Any = None
    # Optional; only "down_payment" is accepted (Cash on Delivery is no longer offered).
    payment_option: Any = None
    reason: Any = ""


class CustomerPaymentSubmission(CustomerOrderAction):
    # Optional: the amount is calculated by the server. A value that differs from the amount due is refused.
    amount: Any = None
    # Optional: customers pay through GCash.
    method: Any = None
    # The GCash reference number.
    reference: Any = ""
    # Required GCash screenshot or receipt: {"name": "...", "dataUrl": "data:image/png;base64,..."}.
    proof: Any = None


class NotificationsRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Notification ids to mark as read; omit to mark all of them.
    ids: Any = None


class AdminQuotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_amount: Any = None
    payment_instructions: Any = ""
    # Cash on Delivery is no longer offered: true is refused.
    cod_allowed: Any = None
    # Optional message shown to the customer with the quotation.
    note: Any = ""
    expected_version: Any = None


class AdminConfirmation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment_instructions: Any = ""
    expected_version: Any = None


class AdminSubmissionReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Verify: the amount actually received (defaults to the reported amount) and the received confirmation.
    amount: Any = None
    received: Any = None
    # Reject: the reason shown to the customer.
    note: Any = ""
    expected_version: Any = None


class AdminPayment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount: Any = None
    received: Any = None
    method: Any = None
    # Required for GCash, Maya, bank transfer, and other providers. For cash: the official receipt number,
    # or blank to have the system issue a cash receipt number.
    reference: Any = ""
    # Optional note kept with the verification record (for example who received the cash).
    verification_note: Any = ""
    expected_version: Any = None


class AdminPaymentReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The missing receipt or transaction number of an older payment.
    reference: Any = ""
    note: Any = ""
    # The administrator confirms the reference was checked against the shop's records.
    confirmed: Any = None
    expected_version: Any = None


class AdminDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: Any = None
    expected_version: Any = None


class AdminOrderStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Any = None
    # Optional reason recorded in the order history (e.g. why a request was rejected).
    note: Any = ""
    expected_version: Any = None
