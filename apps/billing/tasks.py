"""Celery tasks for billing operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

import httpx
import stripe
from asgiref.sync import async_to_sync
from django.db import transaction
from django.db.models import QuerySet
from django.db.utils import OperationalError

from apps.billing.repositories import get_webhook_repos
from config.celery import app

logger = logging.getLogger(__name__)

_FX_API_URL = "https://open.er-api.com/v6/latest/USD"


def _to_minor_units(display_amount: float, currency: str) -> int:
    """Inverse of ``format_amount``: display units → integer minor units.

    Zero-decimal currencies (JPY, KRW, …) are already in whole units; others
    multiply by 100. Goes through :class:`Decimal` with explicit
    ``ROUND_HALF_UP`` so ``round_friendly`` outputs like ``9.99`` (not
    exactly representable in IEEE-754) land on ``999`` minor units instead
    of ``998`` under banker's rounding.
    """
    # Deferred to avoid a circular import at module load (currency module
    # imports from a chain that ends back at apps.billing).
    from saasmint_core.services.currency import ZERO_DECIMAL_CURRENCIES

    amount = Decimal(str(display_amount))
    if currency.lower() not in ZERO_DECIMAL_CURRENCIES:
        amount = amount * 100
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def sync_localized_prices() -> int:
    """Recompute every ``LocalizedPrice`` row from the live FX rate snapshot.

    USD is the source of truth — Stripe always charges USD. This task derives
    a *display* price (friendly-rounded) for every supported non-USD currency
    so the API can serve a stable price tag without per-request FX math. Runs
    daily via Celery Beat and on every deploy via ``infra/entrypoint.sh``.

    Stability gate: ``amount_minor`` is only rewritten when the freshly
    friendly-rounded value differs from the existing row. This prevents tiny
    daily FX moves from churning customer-visible Stripe Prices for billable
    currencies (``sync_stripe_catalog`` re-mints whenever ``amount_minor``
    changes). ``stripe_price_id`` is never touched here — only by
    ``sync_stripe_catalog`` after it observes an ``amount_minor`` change.

    Returns the number of rows that were *changed* (created or whose
    ``amount_minor`` moved). On FX-API failure logs and returns 0 — existing
    rows are preserved so a flaky upstream never erases the catalog.
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

    rates_raw = data.get("rates")
    if not isinstance(rates_raw, dict):
        logger.error("FX API returned non-dict rates payload")
        return 0
    try:
        api_rates: dict[str, float] = {k.lower(): float(v) for k, v in rates_raw.items()}
    except (TypeError, ValueError):
        logger.exception("FX API returned malformed rate values")
        return 0
    now = datetime.now(UTC)

    def _compute_new_amounts(amount: int) -> dict[str, int]:
        """Return ``{currency: new_amount_minor}`` for all non-USD currencies."""
        result: dict[str, int] = {}
        for currency in SUPPORTED_CURRENCIES:
            if currency == "usd":
                continue
            rate = api_rates.get(currency)
            if rate is None:
                logger.warning("No FX rate for currency %s", currency)
                continue
            display = round_friendly(format_amount(amount, "usd") * rate, currency)
            result[currency] = _to_minor_units(display, currency)
        return result

    def _upsert_for_price(
        amount: int,
        *,
        plan_price_id: UUID | None = None,
        product_price_id: UUID | None = None,
        existing_by_currency: dict[str, LocalizedPrice] | None = None,
    ) -> tuple[
        list[LocalizedPrice], list[LocalizedPrice], list[LocalizedPrice], int
    ]:
        """Compute create/update/heartbeat lists for one (price, currencies) pair.

        Reads pre-bucketed existing rows from ``existing_by_currency`` instead of
        firing a SELECT here. Returns ``(to_create, to_update_changed,
        to_update_heartbeat, changed_count)`` so the caller can aggregate across
        all prices and apply bulk_create / bulk_update once at the end.
        """
        # The conditional below guarantees a non-None UUID is selected; the
        # narrower annotation makes the kwarg shape passed to LocalizedPrice
        # explicit (the FK fields it lands on are NOT NULL).
        owner_kwargs: dict[str, UUID]
        if plan_price_id is not None:
            owner_kwargs = {"plan_price_id": plan_price_id}
        else:
            assert product_price_id is not None  # noqa: S101  # exactly-one invariant
            owner_kwargs = {"product_price_id": product_price_id}
        new_amounts = _compute_new_amounts(amount)
        existing_by_currency = existing_by_currency or {}

        to_create: list[LocalizedPrice] = []
        to_update_changed: list[LocalizedPrice] = []
        to_update_heartbeat: list[LocalizedPrice] = []

        for currency, new_amount in new_amounts.items():
            existing = existing_by_currency.get(currency)
            if existing is None:
                to_create.append(
                    LocalizedPrice(
                        currency=currency,
                        amount_minor=new_amount,
                        synced_at=now,
                        **owner_kwargs,
                    )
                )
            elif existing.amount_minor != new_amount:
                existing.amount_minor = new_amount
                existing.synced_at = now
                to_update_changed.append(existing)
            else:
                # Stability gate: friendly-rounded value didn't move. Refresh
                # synced_at as a heartbeat so monitoring can spot a stale sync,
                # but leave amount_minor + stripe_price_id alone.
                existing.synced_at = now
                to_update_heartbeat.append(existing)

        return to_create, to_update_changed, to_update_heartbeat, (
            len(to_create) + len(to_update_changed)
        )

    with transaction.atomic():
        # Two SELECTs total: one for plan-price-owned rows, one for
        # product-price-owned. Bucket by owner id so per-price upserts can do
        # a dict lookup instead of a query.
        plan_buckets: dict[UUID, dict[str, LocalizedPrice]] = {}
        for lp in LocalizedPrice.objects.filter(plan_price_id__isnull=False).only(
            "id", "currency", "amount_minor", "synced_at", "plan_price_id"
        ):
            owner_id = lp.plan_price_id
            assert owner_id is not None  # noqa: S101  # filtered above
            plan_buckets.setdefault(owner_id, {})[lp.currency] = lp

        product_buckets: dict[UUID, dict[str, LocalizedPrice]] = {}
        for lp in LocalizedPrice.objects.filter(product_price_id__isnull=False).only(
            "id", "currency", "amount_minor", "synced_at", "product_price_id"
        ):
            owner_id = lp.product_price_id
            assert owner_id is not None  # noqa: S101  # filtered above
            product_buckets.setdefault(owner_id, {})[lp.currency] = lp

        all_create: list[LocalizedPrice] = []
        all_update_changed: list[LocalizedPrice] = []
        all_update_heartbeat: list[LocalizedPrice] = []
        changed = 0

        sources: list[
            tuple[
                QuerySet[PlanPrice] | QuerySet[ProductPrice],
                str,
                dict[UUID, dict[str, LocalizedPrice]],
            ]
        ] = [
            (PlanPrice.objects.all().only("id", "amount"), "plan_price_id", plan_buckets),
            (
                ProductPrice.objects.all().only("id", "amount"),
                "product_price_id",
                product_buckets,
            ),
        ]
        for queryset, owner_kwarg, buckets in sources:
            for price in queryset:
                create, upd_changed, upd_heartbeat, n = _upsert_for_price(
                    price.amount,
                    existing_by_currency=buckets.get(price.id, {}),
                    **{owner_kwarg: price.id},
                )
                all_create.extend(create)
                all_update_changed.extend(upd_changed)
                all_update_heartbeat.extend(upd_heartbeat)
                changed += n

        if all_create:
            LocalizedPrice.objects.bulk_create(all_create)
        if all_update_changed:
            LocalizedPrice.objects.bulk_update(
                all_update_changed, ["amount_minor", "synced_at"]
            )
        if all_update_heartbeat:
            LocalizedPrice.objects.bulk_update(all_update_heartbeat, ["synced_at"])

    logger.info("Localized prices synced: %d rows changed", changed)
    return changed


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
    import resend.exceptions

    for email in emails:
        try:
            sender(email, subscription_label)
        except (resend.exceptions.ResendError, httpx.HTTPError):
            # A flaky upstream / one bad address must not block notices to the
            # rest of the recipient list. Resend retries are idempotent on our
            # side and the billing state change is authoritative — the email
            # is best-effort. Programming errors are intentionally not caught
            # here so they surface in Sentry.
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
