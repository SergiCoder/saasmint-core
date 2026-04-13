"""Transactional emails for the orgs app using Resend."""

from __future__ import annotations

import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_from_address() -> str:
    return settings.EMAIL_FROM_ADDRESS


def _get_frontend_url() -> str:
    return settings.FRONTEND_URL


def _send(to: str, subject: str, html: str) -> None:
    """Send a single email via Resend."""
    if not resend.api_key:
        resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(
        {
            "from": _get_from_address(),
            "to": [to],
            "subject": subject,
            "html": html,
        }
    )


def send_invitation_email(email: str, token: str, org_name: str, inviter_name: str) -> None:
    """Send an org invitation link."""
    link = f"{_get_frontend_url()}/invitations/{token}"
    _send(
        to=email,
        subject=f"You've been invited to join {org_name}",
        html=(
            f"<p>{inviter_name} has invited you to join <strong>{org_name}</strong>"
            " on SaasMint.</p>"
            f'<p><a href="{link}">Accept Invitation</a></p>'
            "<p>This invitation expires in 7 days.</p>"
        ),
    )
    logger.info("Invitation email sent to %s for org %s", email, org_name)
