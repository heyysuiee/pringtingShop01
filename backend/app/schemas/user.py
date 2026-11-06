from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def normalize_email(value: str) -> str:
    """Lower-case and trim an email address; raise ValueError when it is not a plausible address."""
    normalized = value.strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def validate_phone(value: str) -> str:
    """Raise ValueError unless the contact number has at least seven digits."""
    if len(re.sub(r"\D", "", value)) < 7:
        raise ValueError("Enter a valid contact number.")
    return value


class _EmailModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class _ContactDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=40)
    messenger: str = Field(default="", max_length=120)

    @field_validator("name", "phone", "messenger")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, value: str) -> str:
        return validate_phone(value)


class CustomerRegistration(_ContactDetails, _EmailModel):
    password: str = Field(min_length=12, max_length=256)


class CustomerLogin(_EmailModel):
    password: str = Field(min_length=1, max_length=256)


class CustomerProfileUpdate(_ContactDetails):
    pass


class PasswordResetRequest(_EmailModel):
    pass


class PasswordResetVerify(PasswordResetRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordResetConfirm(PasswordResetRequest):
    reset_token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=12, max_length=256)
