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
    # "down_payment" (50% after the quotation) or "cod" (cash on delivery, no down payment).
    payment_option: Any = None


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


class AdminQuotation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_amount: Any = None
    payment_instructions: Any = ""
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
