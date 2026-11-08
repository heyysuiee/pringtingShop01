from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

from backend.app.core.config import settings


logger = logging.getLogger("printshop.email")


class EmailDeliveryError(RuntimeError):
    """Raised when a message cannot be handed to the configured SMTP server."""


def send_message(recipient: str, subject: str, body: str, html_body: str | None = None) -> None:
    """Send an email through the SMTP server configured in the PRINTSHOP_SMTP_* variables.

    Port 465 uses implicit TLS; other ports use STARTTLS unless PRINTSHOP_SMTP_STARTTLS=false.
    """
    if not settings.smtp_configured:
        logger.error("Email not sent: PRINTSHOP_SMTP_HOST and PRINTSHOP_SMTP_FROM are not set.")
        raise EmailDeliveryError(
            "Password reset emails are not set up on this server yet. The shop administrator must set the PRINTSHOP_SMTP_* variables."
        )
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        if settings.smtp_port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with client:
            client.ehlo()
            if settings.smtp_port != 465 and settings.smtp_starttls:
                client.starttls(context=context)
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        logger.error("SMTP login was rejected by %s", settings.smtp_host)
        raise EmailDeliveryError("The email could not be sent. Please try again later or contact the shop.") from error
    except (smtplib.SMTPException, OSError) as error:
        # Never log the message body or the recipient's code: it is a one-time secret.
        logger.error("SMTP delivery failed: %s", type(error).__name__)
        raise EmailDeliveryError("The email could not be sent. Please try again later or contact the shop.") from error


def send_password_reset_code(recipient: str, name: str, code: str, lifetime_minutes: int) -> None:
    greeting = f"Hello {name}," if name else "Hello,"
    body = (
        f"{greeting}\n\n"
        f"Your ImPRESSion PrintShop verification code is: {code}\n\n"
        f"The code expires in {lifetime_minutes} minutes and can be used once. If you did not request a password reset, "
        "you can ignore this email; your password will not change.\n\n"
        "For your security, never share this code with anyone. ImPRESSion PrintShop staff will never ask for it."
    )
    safe_greeting = html.escape(greeting)
    html_body = f"""\
<!DOCTYPE html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f4f4f4;font-family:Segoe UI,Arial,sans-serif;color:#2b2b2b;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #d4d4d4;border-radius:12px;">
    <tr><td style="padding:28px 32px 8px;">
      <p style="margin:0;font-family:Georgia,serif;font-size:22px;">ImPRESSion PrintShop</p>
      <p style="margin:4px 0 0;font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#5c5c5c;">Password reset</p>
    </td></tr>
    <tr><td style="padding:16px 32px;">
      <p style="margin:0 0 12px;font-size:15px;">{safe_greeting}</p>
      <p style="margin:0 0 16px;font-size:15px;line-height:1.6;">Use this verification code to reset your password:</p>
      <p style="margin:0 0 16px;padding:14px 0;text-align:center;font-size:32px;font-weight:700;letter-spacing:10px;background:#f4f4f4;border-radius:8px;">{html.escape(code)}</p>
      <p style="margin:0 0 12px;font-size:14px;line-height:1.6;">The code expires in {lifetime_minutes} minutes and can be used once.</p>
      <p style="margin:0;font-size:13px;line-height:1.6;color:#5c5c5c;">If you did not request a password reset, you can ignore this email; your password will not change. Never share this code with anyone.</p>
    </td></tr>
    <tr><td style="padding:12px 32px 28px;font-size:12px;color:#5c5c5c;">This is an automated message from ImPRESSion PrintShop.</td></tr>
  </table>
</body>
</html>"""
    send_message(recipient, "Your ImPRESSion PrintShop password reset code", body, html_body)
