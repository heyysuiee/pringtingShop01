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
    # Package orders: "down_payment" (50%) or "cod" (cash on delivery). Customized orders choose after the quotation.
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
    payment_option: Any = None
    reason: Any = ""


class CustomerPaymentSubmission(CustomerOrderAction):
    amount: Any = None
    method: Any = None
    reference: Any = ""
    # Optional screenshot or receipt: {"name": "...", "dataUrl": "data:image/png;base64,..."}.
    proof: Any = None


class AdminQuotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_amount: Any = None
    payment_instructions: Any = ""
    # Whether the customer may choose Cash on Delivery when accepting this quotation.
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
    reference: Any = ""
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
