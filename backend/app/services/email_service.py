from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from backend.app.core.config import settings


logger = logging.getLogger("printshop.email")


class EmailDeliveryError(RuntimeError):
    """Raised when a message cannot be handed to the configured SMTP server."""


def send_message(recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_configured:
        raise EmailDeliveryError(
            "Password reset emails are not set up on this server yet. The shop administrator must set the PRINTSHOP_SMTP_* variables."
        )
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
            client.ehlo()
            if settings.smtp_starttls:
                client.starttls(context=context)
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        # Never log the message body: it may contain a one-time code.
        logger.error("SMTP delivery failed: %s", type(error).__name__)
        raise EmailDeliveryError("The email could not be sent.") from error


def send_password_reset_code(recipient: str, name: str, code: str, lifetime_minutes: int) -> None:
    greeting = f"Hello {name}," if name else "Hello,"
    body = (
        f"{greeting}\n\n"
        f"Your ImPRESSion PrintShop verification code is: {code}\n\n"
        f"The code expires in {lifetime_minutes} minutes. If you did not request a password reset, "
        "you can ignore this email; your password will not change.\n\n"
        "For your security, never share this code with anyone."
    )
    send_message(recipient, "Your ImPRESSion PrintShop password reset code", body)
