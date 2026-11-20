"""Request bodies for the chatbot endpoints. Unknown fields are refused and every text has a length limit."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(_Strict):
    conversation_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=1000)
    page: str = Field(default="", max_length=200)


class ChatInquiry(_Strict):
    conversation_id: str | None = Field(default=None, max_length=64)
    name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    message: str = Field(min_length=1, max_length=2000)


class ChatSettingsUpdate(_Strict):
    enabled: bool | None = None
    welcome_message: str | None = Field(default=None, max_length=600)
    suggestions: list[str] | None = Field(default=None, max_length=8)


class FaqCreate(_Strict):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=3, max_length=3000)
    keywords: str = Field(default="", max_length=300)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    active: bool = True


class FaqUpdate(_Strict):
    question: str | None = Field(default=None, min_length=3, max_length=300)
    answer: str | None = Field(default=None, min_length=3, max_length=3000)
    keywords: str | None = Field(default=None, max_length=300)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    active: bool | None = None


class InquiryStatus(_Strict):
    status: Literal["open", "resolved"]
    note: str = Field(default="", max_length=1000)
