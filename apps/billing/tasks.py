"""Celery tasks for billing operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import stripe
from asgiref.sync import async_to_sync
from django.db import transaction
from django.db.utils import OperationalError

from apps.billing.repositories import get_webhook_repos
from config.celery import app

logger = logging.getLogger(__name__)

_FX_API_URL = "https://open.er-api.com/v6/latest/USD"


def _to_minor_units(display_amount: float, currency: str) -> int:
    """Inverse of ``format_amount``: display units → integer minor units.

    Zero-decimal currencies (JPY, KRW, …) are already in whole units; others
    multiply by 100. ``round`` guards against float drift introduced by
    ``round_friendly`` returning values like ``9.99`` that aren't exactly
    representable in IEEE-754.
    """
    from saasmint_core.services.currency import ZERO_DECIMAL_CURRENCIES

    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return round(display_amount)
    return round(display_amount * 100)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def sync_localized_prices() -> int:
    """Recompute every ``LocalizedPrice`` row from the live FX rate snapshot.

    USD is the source of truth — Stripe always charges USD. This task derives
    a *display* price (friendly-rounded) for every supported non-USD currency
    so the API can serve a stable price tag without per-request FX math. Runs
    daily via Celery Beat and on every deploy via ``infra/entrypoint.sh``.

    Returns the number of rows written (for logging / management command
    output). On API failure logs and returns 0 — existing rows are kept,
    so a flaky upstream never erases the catalog.
    """
    from saasmint_core.services.currency import (
        SUPPORTED_CURRENCIES,
        format_amount,
        round_friendly,
    )

    from apps.billing.models import LocalizedPrice, PlanPrice, ProductPrice

    try:
        resp = httpx.get(_FX_API_URL, timeout=httpx.Timeout(10.0))
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Failed to fetch FX rates from %s", _FX_API_URL)
        return 0

    if data.get("result") != "success":
        logger.error("FX API returned non-success payload: %s", data)
        return 0

    api_rates: dict[str, float] = {k.lower(): v for k, v in data["rates"].items()}
    now = datetime.now(UTC)

    rows: list[LocalizedPrice] = []
    for plan_price in PlanPrice.objects.all().only("id", "amount"):
        for currency in SUPPORTED_CURRENCIES:
            if currency == "usd":
                continue
            rate = api_rates.get(currency)
            if rate is None:
                logger.warning("No FX rate for currency %s", currency)
                continue
            display = round_friendly(format_amount(plan_price.amount, "usd") * rate, currency)
            rows.append(
                LocalizedPrice(
                    plan_price_id=plan_price.id,
                    currency=currency,
                    amount_minor=_to_minor_units(display, currency),
                    synced_at=now,
                )
            )

    for product_price in ProductPrice.objects.all().only("id", "amount"):
        for currency in SUPPORTED_CURRENCIES:
            if currency == "usd":
                continue
            rate = api_rates.get(currency)
            if rate is None:
                continue
            display = round_friendly(format_amount(product_price.amount, "usd") * rate, currency)
            rows.append(
                LocalizedPrice(
                    product_price_id=product_price.id,
                    currency=currency,
                    amount_minor=_to_minor_units(display, currency),
                    synced_at=now,
                )
            )

    if not rows:
        logger.info("No catalog prices found; nothing to localize.")
        return 0

    # Postgres ``ON CONFLICT`` can't target a partial unique index implicitly,
    # so we can't use ``bulk_create(update_conflicts=True)`` against
    # ``LocalizedPrice`` (the XOR ``plan_price``/``product_price`` shape forces
    # *two* partial indexes). The table is bounded — at most
    # ``len(plans+products) * len(SUPPORTED_CURRENCIES)`` rows — so delete-then-
    # insert inside a single transaction is simpler than coalesce-on-conflict
    # gymnastics. Other readers see either the old set or the new set, never a
    # half-empty table.
    with transaction.atomic():
        LocalizedPrice.objects.all().delete()
        LocalizedPrice.objects.bulk_create(rows)
    logger.info("Localized prices synced: %d rows", len(rows))
    return len(rows)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_subscription_cancel_notice_task(
    emails: list[str], subscription_label: str, action: str
) -> None:
    """Fan out a subscription-state email to every recipient.

    ``action`` is one of ``"scheduled"`` (cancellation queued for period end)
    or ``"resumed"`` (previously scheduled cancellation cleared). Iteration is
    sequential — one email per recipient — so a single bad address doesn't
    block the others; Resend calls are idempotent from our side.
    """
    from apps.billing.email import (
        send_subscription_cancel_resumed,
        send_subscription_cancel_scheduled,
    )

    sender = (
        send_subscription_cancel_scheduled
        if action == "scheduled"
        else send_subscription_cancel_resumed
    )
    for email in emails:
        try:
            sender(email, subscription_label)
        except Exception:
            logger.exception("Failed to send billing notice to %s (action=%s)", email, action)


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
