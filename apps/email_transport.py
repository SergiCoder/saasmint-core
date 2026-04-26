"""Shared transactional email transport.

Every app-specific ``email.py`` builds its body and delegates to
:func:`send_email` here so the Resend client, API-key lazy-init, and from
address live in one place.
"""

from __future__ import annotations

import resend
from django.conf import settings


def send_email(
    *,
    to: str,
    subject: str,
    html: str | None = None,
    text: str | None = None,
) -> None:
    """Send a single transactional email via Resend.

    Exactly one of ``html`` or ``text`` must be provided.
    """
    if (html is None) == (text is None):
        raise ValueError("send_email requires exactly one of `html` or `text`")
    if not resend.api_key:
        resend.api_key = settings.RESEND_API_KEY
    if html is not None:
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM_ADDRESS,
                "to": [to],
                "subject": subject,
                "html": html,
            }
        )
    else:
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM_ADDRESS,
                "to": [to],
                "subject": subject,
                "text": text,  # type: ignore[typeddict-item]
            }
        )
