"""Customer support chatbot endpoints.

Public (storefront chat window): configuration, sending a message, restoring the conversation after a
page change, and sending an inquiry to the shop administrator. They are rate limited per client address
with the database-backed limiter, so the limits hold across restarts and across Vercel's instances.

Administrator (AI Chatbot page): activity overview, conversations, unanswered questions, inquiries,
FAQs, and settings. Every one requires an administrator session.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from backend.app.core.dependencies import require_admin
from backend.app.core.security import require_same_origin
from backend.app.repositories import auth_throttle_repository as throttle_store
from backend.app.routes.auth import _client_address, _throttle
from backend.app.schemas.chatbot import ChatInquiry, ChatMessage, ChatSettingsUpdate, FaqCreate, FaqUpdate, InquiryStatus
from backend.app.services import chatbot_services


router = APIRouter(tags=["chatbot"])

# Per client address. A conversation is a few questions; these stop scripted floods and runaway AI costs.
CHAT_BURST_LIMIT = (15, 60)            # 15 messages a minute
CHAT_DAILY_LIMIT = (300, 24 * 60 * 60)  # 300 messages a day
INQUIRY_LIMIT = (5, 60 * 60)            # 5 inquiries an hour


# ---------------------------------------------------------------- public


@router.get("/api/chatbot/config")
def chatbot_config(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return chatbot_services.public_config(request)


@router.post("/api/chatbot/messages")
def chatbot_message(payload: ChatMessage, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    address = _client_address(request) or (f"conversation:{payload.conversation_id}" if payload.conversation_id else None)
    _throttle("chat-burst", address, CHAT_BURST_LIMIT, "You're sending messages very quickly. Please wait a moment and try again.", throttle_store)
    _throttle("chat-daily", address, CHAT_DAILY_LIMIT, "The chat limit for today has been reached. Please contact the shop directly.", throttle_store)
    return chatbot_services.chat(payload.model_dump(), request)


@router.get("/api/chatbot/conversations/{conversation_id}")
def chatbot_history(conversation_id: str, request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return chatbot_services.conversation_history(conversation_id, request)


@router.post("/api/chatbot/inquiries", status_code=201)
def chatbot_inquiry(payload: ChatInquiry, request: Request) -> dict[str, Any]:
    require_same_origin(request)
    address = _client_address(request) or (f"conversation:{payload.conversation_id}" if payload.conversation_id else None)
    _throttle("chat-inquiry", address, INQUIRY_LIMIT, "Too many messages were sent. Please try again later.", throttle_store)
    return chatbot_services.submit_inquiry(payload.model_dump(), request)


# ---------------------------------------------------------------- administrator


@router.get("/api/admin/chatbot/overview")
def admin_chatbot_overview(request: Request, response: Response) -> dict[str, Any]:
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return chatbot_services.admin_overview()


@router.get("/api/admin/chatbot/conversations")
def admin_chatbot_conversations(request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_conversations(dict(request.query_params))


@router.get("/api/admin/chatbot/conversations/{conversation_id}")
def admin_chatbot_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_conversation(conversation_id)


@router.delete("/api/admin/chatbot/conversations/{conversation_id}", status_code=204)
def admin_chatbot_delete_conversation(conversation_id: str, request: Request) -> Response:
    require_admin(request)
    chatbot_services.admin_delete_conversation(conversation_id)
    return Response(status_code=204)


@router.get("/api/admin/chatbot/unanswered")
def admin_chatbot_unanswered(request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_unanswered(dict(request.query_params))


@router.post("/api/admin/chatbot/unanswered/{message_id}/review")
def admin_chatbot_review(message_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_mark_reviewed(message_id)


@router.get("/api/admin/chatbot/inquiries")
def admin_chatbot_inquiries(request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_inquiries(dict(request.query_params))


@router.post("/api/admin/chatbot/inquiries/{inquiry_id}/status")
def admin_chatbot_inquiry_status(inquiry_id: str, payload: InquiryStatus, request: Request) -> dict[str, Any]:
    admin = require_admin(request)
    return chatbot_services.admin_set_inquiry_status(inquiry_id, payload.status, payload.note, admin["sub"])


@router.get("/api/admin/chatbot/faqs")
def admin_chatbot_faqs(request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_faqs()


@router.post("/api/admin/chatbot/faqs", status_code=201)
def admin_chatbot_create_faq(payload: FaqCreate, request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_create_faq(payload.model_dump())


@router.patch("/api/admin/chatbot/faqs/{faq_id}")
def admin_chatbot_update_faq(faq_id: str, payload: FaqUpdate, request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_update_faq(faq_id, payload.model_dump(exclude_unset=True))


@router.delete("/api/admin/chatbot/faqs/{faq_id}", status_code=204)
def admin_chatbot_delete_faq(faq_id: str, request: Request) -> Response:
    require_admin(request)
    chatbot_services.admin_delete_faq(faq_id)
    return Response(status_code=204)


@router.get("/api/admin/chatbot/settings")
def admin_chatbot_settings(request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.admin_settings()


@router.patch("/api/admin/chatbot/settings")
def admin_chatbot_update_settings(payload: ChatSettingsUpdate, request: Request) -> dict[str, Any]:
    require_admin(request)
    return chatbot_services.update_settings(payload.model_dump(exclude_unset=True))
