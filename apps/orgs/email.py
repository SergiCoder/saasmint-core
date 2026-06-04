"""Transactional emails for the orgs app using Resend."""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils.html import escape

from apps.email_transport import send_email

logger = logging.getLogger(__name__)


def send_invitation_email(email: str, token: str, org_name: str, inviter_name: str) -> None:
    """Send an org invitation link.

    ``org_name`` and ``inviter_name`` are user-controlled (an admin sets the
    org name; the inviter's full_name is set at registration). They get
    interpolated into raw HTML below, so we escape them to keep stray
    angle brackets / quotes out of the rendered email — otherwise an org
    named ``<script>...`` or an inviter named ``"><img onerror=...`` could
    smuggle markup into the recipient's mail client.
    """
    link = f"{settings.FRONTEND_URL}/invitations/{token}"
    safe_org = escape(org_name)
    safe_inviter = escape(inviter_name)
    send_email(
        to=email,
        subject=f"You've been invited to join {org_name}",
        html=(
            f"<p>{safe_inviter} has invited you to join <strong>{safe_org}</strong>"
            " on SaasMint.</p>"
            f'<p><a href="{link}">Accept Invitation</a></p>'
            "<p>This invitation expires in 7 days.</p>"
        ),
    )
    logger.info("Invitation email sent to %s for org %s", email, org_name)
