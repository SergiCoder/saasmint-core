"""Celery tasks for billing operations."""

from __future__ import annotations

import json
import logging

import stripe
from asgiref.sync import async_to_sync
from django.conf import settings
from django.db.utils import OperationalError

from apps.billing.repositories import get_webhook_repos
from config.celery import app

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]  # celery has no stubs
def process_stripe_webhook(self: object, payload: str, signature: str) -> None:
    """Process a Stripe webhook event asynchronously with retry on failure."""
    from saasmint_core.exceptions import WebhookVerificationError
    from saasmint_core.services.webhooks import handle_stripe_event

    repos = get_webhook_repos()

    # Extract event metadata for structured logging
    try:
        event_data = json.loads(payload)
        event_id = event_data.get("id", "unknown")
        event_type = event_data.get("type", "unknown")
    except (json.JSONDecodeError, TypeError):
        event_id = "unknown"
        event_type = "unknown"

    try:
        async_to_sync(handle_stripe_event)(
            payload=payload.encode("utf-8"),
            signature=signature,
            webhook_secret=settings.STRIPE_WEBHOOK_SECRET,
            repos=repos,
        )
    except WebhookVerificationError:
        logger.error("Webhook signature verification failed for event %s — not retrying.", event_id)
        raise
    except (stripe.StripeError, ConnectionError, OperationalError) as exc:
        logger.exception(
            "Webhook processing failed for event %s (type=%s), retrying: %s",
            event_id,
            event_type,
            exc,
        )
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc  # type: ignore[attr-defined]  # self is typed as object; retry/request attrs are injected by Celery at runtime
