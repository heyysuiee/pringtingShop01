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


# Customer password policy (registration and password reset). The same rules are shown as a live
# checklist on the registration and reset pages (frontend/assets/auth.js).
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256


def password_problems(value: str) -> list[str]:
    """Every customer password rule ``value`` breaks, as messages; an empty list means it is valid."""
    problems = []
    if len(value) < PASSWORD_MIN_LENGTH:
        problems.append(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    if not re.search(r"[A-Za-z]", value):
        problems.append("Include at least one letter.")
    if not re.search(r"\d", value):
        problems.append("Include at least one number.")
    if re.search(r"\s", value):
        problems.append("Do not use spaces.")
    return problems


def validate_password(value: str) -> str:
    """Raise ValueError (one message per broken rule) unless the password meets the customer policy."""
    problems = password_problems(value)
    if problems:
        raise ValueError("Password does not meet the requirements: " + " ".join(problems))
    return value


def validate_phone(value: str) -> str:
    """Raise ValueError unless the number is a plausible Philippine mobile or landline.

    Accepts the formats customers actually type: ``09XX XXX XXXX``, ``+63 9XX XXX XXXX``,
    ``639XXXXXXXXX``, and landlines such as ``02 1234 5678``. Spaces, dashes, and parentheses
    are ignored; the original formatting is kept for display.
    """
    digits = re.sub(r"\D", "", value)
    mobile = re.fullmatch(r"(?:0|63)9\d{9}", digits)
    landline = re.fullmatch(r"0\d{7,9}", digits)
    if mobile is None and landline is None:
        raise ValueError("Enter a valid Philippine mobile or landline number.")
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
    # Length is checked by validate_password so every broken rule is reported together.
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_password(value)


class CustomerLogin(_EmailModel):
    password: str = Field(min_length=1, max_length=256)
    # "Remember me": keep the customer signed in for 30 days instead of the browser session.
    remember: bool = False


class CustomerProfileUpdate(_ContactDetails):
    pass


class CustomerDeliveryAddress(BaseModel):
    """A reusable address saved on the customer account, validated by auth_services/PSGC."""

    model_config = ConfigDict(extra="forbid")

    recipient_name: str = Field(min_length=1, max_length=120)
    contact_number: str = Field(min_length=7, max_length=40)
    street: str = Field(min_length=1, max_length=200)
    barangay: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=120)
    province: str = Field(min_length=1, max_length=120)
    landmark: str = Field(default="", max_length=200)
    delivery_instructions: str = Field(default="", max_length=500)


class EmailVerificationRequest(_EmailModel):
    pass


class EmailVerificationVerify(EmailVerificationRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordResetRequest(_EmailModel):
    pass


class PasswordResetVerify(PasswordResetRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordResetConfirm(PasswordResetRequest):
    reset_token: str = Field(min_length=20, max_length=200)
    # The customer and administrator services apply their own password rules to this value.
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
