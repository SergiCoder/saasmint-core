"""Transactional billing emails using Resend."""

from __future__ import annotations

import logging

from django.utils.html import escape

from apps.email_transport import send_email

logger = logging.getLogger(__name__)


def send_subscription_cancel_scheduled(email: str, subscription_label: str) -> None:
    """Notify that a subscription has been scheduled to cancel at period end.

    ``subscription_label`` is derived from a user-editable plan/org name,
    so it gets escaped before being interpolated into the HTML body to
    prevent stray markup from showing up in the recipient's mail client.
    """
    safe_label = escape(subscription_label)
    send_email(
        to=email,
        subject="Your subscription is scheduled to cancel",
        html=(
            f"<p>The subscription for <strong>{safe_label}</strong> has been"
            " scheduled to cancel at the end of the current billing period.</p>"
            "<p>You can resume it before the period ends from your billing settings.</p>"
        ),
    )
    logger.info("Subscription cancel-scheduled email sent to %s", email)


def send_subscription_cancel_resumed(email: str, subscription_label: str) -> None:
    """Notify that a previously scheduled cancellation has been cleared.

    ``subscription_label`` is escaped — see :func:`send_subscription_cancel_scheduled`.
    """
    safe_label = escape(subscription_label)
    send_email(
        to=email,
        subject="Your subscription cancellation was reverted",
        html=(
            f"<p>The scheduled cancellation for <strong>{safe_label}</strong>"
            " has been cleared. The subscription will continue to renew.</p>"
        ),
    )
    logger.info("Subscription cancel-resumed email sent to %s", email)
