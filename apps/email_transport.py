"""Shared transactional email transport.

Every app-specific ``email.py`` builds its HTML/subject and delegates to
:func:`send_email` here so the Resend client, API-key lazy-init, and from
address live in one place.
"""

from __future__ import annotations

import resend
from django.conf import settings


def send_email(*, to: str, subject: str, html: str) -> None:
    """Send a single transactional email via Resend."""
    if not resend.api_key:
        resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(
        {
            "from": settings.EMAIL_FROM_ADDRESS,
            "to": [to],
            "subject": subject,
            "html": html,
        }
    )
