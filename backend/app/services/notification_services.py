"""Customer notifications about their orders.

A notification is stored in the database together with the order change that caused it (see
order_repository.update_order), so it is shown in the customer portal even when email is not set up.
When SMTP is configured, each newly stored notification is also emailed once, in the background.
Notifications are keyed by ``dedupe_key``: the same event never notifies the customer twice.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.repositories import notification_repository


logger = logging.getLogger("printshop.notifications")


def build(order: Mapping[str, Any], kind: str, dedupe_key: str, title: str, message: str) -> dict[str, Any]:
    return {
        "dedupe_key": f"{order['reference']}:{dedupe_key}",
        "customer_id": order.get("customer_id"),
        "customer_email": str((order.get("customer") or {}).get("email") or ""),
        "kind": kind,
        "title": title,
        "message": message,
    }


def _send_email(recipient: str, subject: str, body: str) -> None:
    # Imported here so the tests' email stub (which patches email_service) is used.
    from backend.app.services import email_service

    try:
        email_service.send_message(recipient, subject, body)
    except Exception:  # noqa: BLE001 - delivery problems must never undo or block the order change
        logger.warning("Order notification email could not be sent.")


def deliver(reference: str, notifications: list[dict[str, Any]]) -> None:
    """Email the notifications that were newly stored (skipped duplicates are never re-sent)."""
    if not settings.smtp_configured:
        return
    for notification in notifications:
        if not notification.get("created") or not notification.get("customer_email"):
            continue
        body = (
            f"{notification['message']}\n\n"
            f"Order reference: {reference}\n"
            "Sign in to the ImPRESS Print Works customer portal to see the details.\n\n"
            "This is an automated message from ImPRESS Print Works."
        )
        threading.Thread(
            target=_send_email,
            args=(notification["customer_email"], f"{notification['title']} — {reference}", body),
            daemon=True,
        ).start()


def customer_notifications(customer_id: str) -> dict[str, Any]:
    return notification_repository.list_for_customer(customer_id)


def mark_read(customer_id: str, ids: Any) -> dict[str, Any]:
    if ids is not None:
        if not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(item, str) and len(item) <= 64 for item in ids):
            raise HTTPException(status_code=422, detail="Invalid notification list.")
    notification_repository.mark_read(customer_id, ids)
    return notification_repository.list_for_customer(customer_id)
