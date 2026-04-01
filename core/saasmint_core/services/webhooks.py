"""Idempotent Stripe webhook handler — stores every event before processing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import stripe

from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import Subscription, SubscriptionStatus
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.plan import PlanRepository
from saasmint_core.repositories.stripe_event import StripeEventRepository
from saasmint_core.repositories.subscription import SubscriptionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookRepos:
    events: StripeEventRepository
    subscriptions: SubscriptionRepository
    customers: StripeCustomerRepository
    plans: PlanRepository


async def handle_stripe_event(
    payload: bytes,
    signature: str,
    webhook_secret: str,
    repos: WebhookRepos,
) -> None:
    """
    Verify, deduplicate, store, and dispatch a Stripe webhook event.

    Raises WebhookVerificationError if the signature is invalid.
    Is a no-op if the event has already been processed (idempotent).
    """
    from saasmint_core.exceptions import WebhookVerificationError

    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)  # type: ignore[no-untyped-call]  # Stripe stub missing return type annotation
    except stripe.SignatureVerificationError as exc:
        raise WebhookVerificationError("Invalid Stripe webhook signature") from exc

    stripe_id: str = event["id"]

    is_new = await repos.events.save_if_new(
        StripeEvent(
            id=uuid4(),
            stripe_id=stripe_id,
            type=event["type"],
            livemode=event["livemode"],
            payload=dict(event),
            created_at=datetime.now(UTC),
        )
    )
    if not is_new:
        logger.info("Skipping duplicate Stripe event %s", stripe_id)
        return

    try:
        await _dispatch(event, repos)
        await repos.events.mark_processed(stripe_id)
    except Exception as exc:
        await repos.events.mark_failed(stripe_id, str(exc))
        logger.exception("Failed to process Stripe event %s: %s", stripe_id, exc)
        raise


async def _dispatch(event: stripe.Event, repos: WebhookRepos) -> None:
    match event["type"]:
        case "customer.subscription.created" | "customer.subscription.updated":
            await _sync_subscription(event["data"]["object"], repos)
        case "customer.subscription.deleted":
            await _on_subscription_deleted(event["data"]["object"], repos)
        case "invoice.payment_succeeded":
            await _on_invoice_paid(event["data"]["object"])
        case "invoice.payment_failed":
            await _on_invoice_failed(event["data"]["object"])
        case _:
            logger.debug("Unhandled Stripe event type: %s", event["type"])


def _extract_discount(sub_data: dict[str, Any]) -> tuple[str | None, float | None, datetime | None]:
    """Extract promotion code, discount percent, and discount end from raw sub data."""
    discount: Any = sub_data.get("discount")
    if not discount:
        return None, None, None

    raw_promo = discount.get("promotion_code")
    promotion_code_id = str(raw_promo) if raw_promo else None

    coupon: dict[str, Any] = cast(dict[str, Any], discount.get("coupon") or {})
    discount_percent = float(coupon["percent_off"]) if coupon.get("percent_off") else None

    raw_end = discount.get("end")
    discount_end_at = datetime.fromtimestamp(int(raw_end), tz=UTC) if raw_end is not None else None

    return promotion_code_id, discount_percent, discount_end_at


def _ts_to_dt(value: int | float | None) -> datetime | None:
    """Convert an optional Unix timestamp to a UTC datetime."""
    return datetime.fromtimestamp(int(value), tz=UTC) if value is not None else None


def _ts_to_dt_required(value: int | float) -> datetime:
    """Convert a Unix timestamp to a UTC datetime (required field)."""
    return datetime.fromtimestamp(int(value), tz=UTC)


async def _sync_subscription(sub_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Upsert a Stripe subscription into the local DB from raw event data."""
    from saasmint_core.exceptions import WebhookDataError

    stripe_customer_str = str(sub_data["customer"])
    items = sub_data["items"]["data"]
    if len(items) > 1:
        logger.warning(
            "Subscription %s has %d line items; only the first is synced",
            sub_data["id"],
            len(items),
        )
    first_item: dict[str, Any] = items[0]
    price_id = str(first_item["price"]["id"])
    stripe_sub_id = str(sub_data["id"])

    customer, plan_price, existing = await asyncio.gather(
        repos.customers.get_by_stripe_id(stripe_customer_str),
        repos.plans.get_price_by_stripe_id(price_id),
        repos.subscriptions.get_by_stripe_id(stripe_sub_id),
    )

    if customer is None:
        logger.warning("Received subscription event for unknown customer %s", stripe_customer_str)
        raise WebhookDataError(f"Unknown customer {stripe_customer_str}")

    if plan_price is None:
        logger.warning("Received subscription event for unknown price %s", price_id)
        raise WebhookDataError(f"Unknown price {price_id}")
    promotion_code_id, discount_percent, discount_end_at = _extract_discount(sub_data)

    subscription = Subscription(
        id=existing.id if existing else uuid4(),
        stripe_id=stripe_sub_id,
        stripe_customer_id=customer.id,
        status=SubscriptionStatus(str(sub_data["status"])),
        plan_id=plan_price.plan_id,
        quantity=int(first_item.get("quantity") or 1),
        promotion_code_id=promotion_code_id,
        discount_percent=discount_percent,
        discount_end_at=discount_end_at,
        trial_ends_at=_ts_to_dt(sub_data.get("trial_end")),
        current_period_start=_ts_to_dt_required(sub_data["current_period_start"]),
        current_period_end=_ts_to_dt_required(sub_data["current_period_end"]),
        canceled_at=_ts_to_dt(sub_data.get("canceled_at")),
        created_at=existing.created_at if existing else datetime.now(UTC),
    )

    await repos.subscriptions.save(subscription)


async def _on_subscription_deleted(sub_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Mark a subscription as canceled when Stripe hard-deletes it."""
    stripe_sub_id = str(sub_data["id"])
    existing = await repos.subscriptions.get_by_stripe_id(stripe_sub_id)
    if existing is None:
        logger.warning("Received deletion event for unknown subscription %s", stripe_sub_id)
        return

    canceled = existing.model_copy(
        update={
            "status": SubscriptionStatus.CANCELED,
            "canceled_at": datetime.now(UTC),
        }
    )
    await repos.subscriptions.save(canceled)


async def _on_invoice_paid(invoice_data: dict[str, Any]) -> None:
    # TODO: persist Invoice record, send receipt email via Celery task
    logger.info("Invoice paid: %s", invoice_data.get("id"))


async def _on_invoice_failed(invoice_data: dict[str, Any]) -> None:
    # TODO: trigger dunning email via Celery task
    # Subscription status (past_due / unpaid) is synced via the
    # customer.subscription.updated event Stripe fires alongside this one.
    logger.warning("Invoice payment failed: %s", invoice_data.get("id"))
