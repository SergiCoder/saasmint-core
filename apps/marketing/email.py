"""Marketing-inquiry email rendering and dispatch via Resend (plain text)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _redact_email(email: str) -> str:
    """Mask the local-part for PII-safe logging: ``jane@example.com`` → ``j***@example.com``."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def send_marketing_inquiry_email(*, to: str, source: str, sender: str, message: str) -> None:
    """Send a marketing-inquiry notification as plain text via Resend."""
    if not resend.api_key:
        resend.api_key = settings.RESEND_API_KEY

    timestamp = datetime.now(UTC).isoformat()
    subject = f"[SaaSmint] {source}: {sender}"
    body = (
        f"Source: {source}\n"
        f"From:   {sender}\n"
        f"At:     {timestamp}\n"
        "\n"
        f"{message if message else '(no message)'}\n"
    )

    resend.Emails.send(
        {
            "from": settings.EMAIL_FROM_ADDRESS,
            "to": [to],
            "subject": subject,
            "text": body,
        }
    )
    logger.info("Marketing inquiry forwarded (source=%s, from=%s)", source, _redact_email(sender))
