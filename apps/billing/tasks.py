"""Celery tasks for billing operations."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import stripe
from asgiref.sync import async_to_sync
from django.conf import settings
from django.db.utils import OperationalError

from apps.billing.repositories import get_webhook_repos
from config.celery import app

logger = logging.getLogger(__name__)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def sync_exchange_rates() -> None:
    """Fetch USD-based exchange rates from Stripe and persist to DB."""
    from saasmint_core.services.currency import SUPPORTED_CURRENCIES

    from apps.billing.models import ExchangeRate

    try:
        rates_obj = stripe.ExchangeRate.retrieve("usd")
    except stripe.StripeError:
        logger.exception("Failed to fetch exchange rates from Stripe")
        return

    now = datetime.now(UTC)
    rates: dict[str, object] = dict(rates_obj.rates)  # type: ignore[arg-type]  # Stripe stubs type mismatch

    updated = 0
    for currency in SUPPORTED_CURRENCIES:
        if currency == "usd":
            continue
        rate = rates.get(currency)
        if rate is None:
            logger.warning("No rate returned by Stripe for currency: %s", currency)
            continue
        ExchangeRate.objects.update_or_create(
            currency=currency,
            defaults={"rate": rate, "fetched_at": now},
        )
        updated += 1
    logger.info("Exchange rates synced: %d currencies updated", updated)


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
