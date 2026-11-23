from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _text(value: Any, label: str, limit: int) -> Any:
    """Trim and bound text supplied to an order action, leaving omitted values to the service rules.

    The services still decide which fields are required and whether a value is allowed for the current
    order state; this check only stops malformed types and oversized strings before they reach them.
    """
    if value is None:
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return value


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

    @field_validator("email")
    @classmethod
    def _clean_email(cls, value: Any) -> Any:
        return _text(value, "Email", 254)


class CustomerOrderChanges(CustomerOrderAction):
    note: Any = ""

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        # Matches request_draft_changes() in order_services.py and the portal textarea.
        return _text(value, "Note", 1000)


class CustomerQuotationResponse(CustomerOrderAction):
    # The quotation revision the customer saw, so a revised quotation is never accepted unseen.
    quotation_revision: Any = None
    # Optional; only "down_payment" is accepted (Cash on Delivery is no longer offered).
    payment_option: Any = None
    reason: Any = ""

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: Any) -> Any:
        return _text(value, "Reason", 500)


class CustomerPaymentSubmission(CustomerOrderAction):
    # Optional: the amount is calculated by the server. A value that differs from the amount due is refused.
    amount: Any = None
    # Optional: customers pay through GCash.
    method: Any = None
    # The GCash reference number.
    reference: Any = ""
    # Required GCash screenshot or receipt: {"name": "...", "dataUrl": "data:image/png;base64,..."}.
    proof: Any = None

    @field_validator("method")
    @classmethod
    def _clean_method(cls, value: Any) -> Any:
        return _text(value, "Payment method", 60)

    @field_validator("reference")
    @classmethod
    def _clean_reference(cls, value: Any) -> Any:
        return _text(value, "Payment reference", 120)


class CustomerPaymentScan(CustomerPaymentSubmission):
    """Pre-submission receipt scan. The result is advisory; the authoritative scan runs on submit."""


class AdminPaymentNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: Any = ""
    expected_version: Any = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        return _text(value, "Verification note", 500)


class AdminReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: Any = ""
    expected_version: Any = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        return _text(value, "Note", 500)


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

    @field_validator("payment_instructions")
    @classmethod
    def _clean_instructions(cls, value: Any) -> Any:
        # Matches set_quotation() and confirm_package_order() in order_services.py.
        return _text(value, "Payment instructions", 1000)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        return _text(value, "Note", 1000)


class AdminConfirmation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment_instructions: Any = ""
    expected_version: Any = None

    @field_validator("payment_instructions")
    @classmethod
    def _clean_instructions(cls, value: Any) -> Any:
        return _text(value, "Payment instructions", 1000)


class AdminSubmissionReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Verify: the amount actually received (defaults to the reported amount) and the received confirmation.
    amount: Any = None
    received: Any = None
    # Reject: the reason shown to the customer.
    note: Any = ""
    expected_version: Any = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        return _text(value, "Reason", 500)


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

    @field_validator("method")
    @classmethod
    def _clean_method(cls, value: Any) -> Any:
        return _text(value, "Payment method", 60)

    @field_validator("reference")
    @classmethod
    def _clean_reference(cls, value: Any) -> Any:
        return _text(value, "Payment reference", 120)

    @field_validator("verification_note")
    @classmethod
    def _clean_verification_note(cls, value: Any) -> Any:
        return _text(value, "Verification note", 300)


class AdminPaymentReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The missing receipt or transaction number of an older payment.
    reference: Any = ""
    note: Any = ""
    # The administrator confirms the reference was checked against the shop's records.
    confirmed: Any = None
    expected_version: Any = None

    @field_validator("reference")
    @classmethod
    def _clean_reference(cls, value: Any) -> Any:
        return _text(value, "Payment reference", 120)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        # Matches record_payment_reference() in order_services.py.
        return _text(value, "Note", 300)


class AdminDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: Any = None
    # Design remarks shown to the customer with the draft.
    remarks: Any = ""
    expected_version: Any = None

    @field_validator("remarks")
    @classmethod
    def _clean_remarks(cls, value: Any) -> Any:
        return _text(value, "Design remarks", 1000)


class AdminOrderStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Any = None
    # Optional reason recorded in the order history (e.g. why a request was rejected).
    note: Any = ""
    expected_version: Any = None

    @field_validator("status")
    @classmethod
    def _clean_status(cls, value: Any) -> Any:
        return _text(value, "Order status", 60)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: Any) -> Any:
        return _text(value, "Reason", 500)
