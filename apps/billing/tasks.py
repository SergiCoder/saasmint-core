"""Celery tasks for billing operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import stripe
from asgiref.sync import async_to_sync
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
    # Stripe stubs type ``ExchangeRate.rates`` as ``float``; the runtime value is
    # a ``dict[str, float]``. Cast once, then iterate — the audit flagged that
    # materialising a whole intermediate ``dict`` via ``dict(rates_obj.rates)``
    # was wasteful when we only read each currency once.
    raw_rates: dict[str, float] = rates_obj.rates  # type: ignore[assignment]  # Stripe stubs type mismatch

    rows: list[ExchangeRate] = []
    for currency in SUPPORTED_CURRENCIES:
        if currency == "usd":
            continue
        rate = raw_rates.get(currency)
        if rate is None:
            logger.warning("No rate returned by Stripe for currency: %s", currency)
            continue
        rows.append(ExchangeRate(currency=currency, rate=Decimal(str(rate)), fetched_at=now))

    if rows:
        ExchangeRate.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["currency"],
            update_fields=["rate", "fetched_at"],
        )
    logger.info("Exchange rates synced: %d currencies updated", len(rows))


@app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]  # celery has no stubs
def process_stripe_webhook(self: object, stripe_event_id: str) -> None:
    """Dispatch a Stripe webhook event that was verified and persisted by the view.

    The view writes the verified payload to ``StripeEvent`` before enqueueing;
    this task looks it up by UUID, routes it through core, and retries only
    transient failures. Keeping the payload in the DB (not the Celery arg)
    avoids PII in Redis and lets retries survive webhook-secret rotation.
    """
    from saasmint_core.exceptions import WebhookDataError
    from saasmint_core.services.webhooks import process_stored_event

    from apps.billing.models import StripeEvent as StripeEventModel

    event_row = StripeEventModel.objects.get(id=stripe_event_id)
    repos = get_webhook_repos()

    try:
        async_to_sync(process_stored_event)(
            event=event_row.payload,
            stripe_id=event_row.stripe_id,
            repos=repos,
        )
    except WebhookDataError as exc:
        logger.error(
            "Webhook permanent error for event %s (type=%s): %s — not retrying.",
            event_row.stripe_id,
            event_row.type,
            exc,
        )
        raise
    except (stripe.StripeError, ConnectionError, OperationalError) as exc:
        logger.exception(
            "Webhook processing failed for event %s (type=%s), retrying: %s",
            event_row.stripe_id,
            event_row.type,
            exc,
        )
        raise self.retry(exc=exc, countdown=2**self.request.retries) from exc  # type: ignore[attr-defined]  # self is typed as object; retry/request attrs are injected by Celery at runtime
