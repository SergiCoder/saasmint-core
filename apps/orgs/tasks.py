"""Celery tasks for the orgs app."""

from __future__ import annotations

import logging

from config.celery import app

logger = logging.getLogger(__name__)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_invitation_email_task(email: str, token: str, org_name: str, inviter_name: str) -> None:
    """Send an org invitation email via Resend (async-safe)."""
    from apps.orgs.email import send_invitation_email

    send_invitation_email(email, token, org_name, inviter_name)
