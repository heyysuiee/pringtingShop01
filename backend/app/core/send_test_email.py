"""Send a test email to check the SMTP settings.

Usage (from the project root, with the PRINTSHOP_SMTP_* variables set or in .env):

    python -m backend.app.core.send_test_email you@example.com
"""
from __future__ import annotations

import sys

from backend.app.core.config import settings
from backend.app.services import email_service


def main() -> int:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print("Usage: python -m backend.app.core.send_test_email you@example.com")
        return 2
    if not settings.smtp_configured:
        print("SMTP is not configured. Set PRINTSHOP_SMTP_HOST and PRINTSHOP_SMTP_FROM (and the username/password if required).")
        return 1
    print(f"Sending a test email through {settings.smtp_host}:{settings.smtp_port} as {settings.smtp_from} ...")
    try:
        email_service.send_message(
            sys.argv[1],
            "ImPRESSion PrintShop test email",
            "This is a test email from ImPRESSion PrintShop. Password reset codes will be sent the same way.",
        )
    except email_service.EmailDeliveryError as error:
        cause = error.__cause__
        print(f"Failed: {type(cause).__name__ if cause else ''} {cause or error}")
        return 1
    print("Sent. Check the inbox (and the spam folder) of", sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
