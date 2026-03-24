"""Idempotent Stripe webhook handler — stores every event before processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import stripe

from stripe_saas_core.domain.stripe_event import StripeEvent
from stripe_saas_core.domain.subscription import Subscription, SubscriptionStatus
from stripe_saas_core.repositories.customer import StripeCustomerRepository
from stripe_saas_core.repositories.plan import PlanRepository
from stripe_saas_core.repositories.stripe_event import StripeEventRepository
from stripe_saas_core.repositories.subscription import SubscriptionRepository

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
    from stripe_saas_core.exceptions import WebhookVerificationError

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


async def _sync_subscription(sub_data: dict[str, object], repos: WebhookRepos) -> None:
    """Upsert a Stripe subscription into the local DB from raw event data."""
    stripe_customer_str = str(sub_data["customer"])
    customer = await repos.customers.get_by_stripe_id(stripe_customer_str)
    if customer is None:
        msg = f"Unknown customer {stripe_customer_str}"
        logger.warning("Received subscription event for %s", msg)
        raise ValueError(msg)

    items_data: list[Any] = sub_data["items"]["data"]  # type: ignore[index]  # sub_data is dict[str, object]; nested indexing requires ignoring object subscript
    first_item: dict[str, Any] = cast(dict[str, Any], items_data[0])
    price_id = str(first_item["price"]["id"])
    plan_price = await repos.plans.get_price_by_stripe_id(price_id)
    if plan_price is None:
        msg = f"Unknown price {price_id}"
        logger.warning("Received subscription event for %s", msg)
        raise ValueError(msg)

    stripe_sub_id = str(sub_data["id"])
    existing = await repos.subscriptions.get_by_stripe_id(stripe_sub_id)

    # Extract optional discount fields
    discount: Any = sub_data.get("discount")
    promotion_code_id: str | None = None
    discount_percent: int | None = None
    discount_end_at: datetime | None = None
    if discount:
        raw_promo = discount.get("promotion_code")
        promotion_code_id = str(raw_promo) if raw_promo else None
        coupon: dict[str, Any] = cast(dict[str, Any], discount.get("coupon") or {})
        if coupon.get("percent_off"):
            discount_percent = int(coupon["percent_off"])
        raw_end = discount.get("end")
        if raw_end:
            discount_end_at = datetime.fromtimestamp(int(raw_end), tz=UTC)

    trial_end = sub_data.get("trial_end")
    canceled_at_ts = sub_data.get("canceled_at")

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
        trial_ends_at=(
            datetime.fromtimestamp(int(trial_end), tz=UTC) if trial_end else None  # type: ignore[call-overload]  # trial_end is object; int(object) doesn't match int(SupportsInt) overload
        ),
        current_period_start=datetime.fromtimestamp(
            int(sub_data["current_period_start"]),  # type: ignore[call-overload]  # value is object; int(object) doesn't match overload
            tz=UTC,
        ),
        current_period_end=datetime.fromtimestamp(
            int(sub_data["current_period_end"]),  # type: ignore[call-overload]  # same: object from dict[str, object]
            tz=UTC,
        ),
        canceled_at=(
            datetime.fromtimestamp(int(canceled_at_ts), tz=UTC)  # type: ignore[call-overload]  # same: object from dict[str, object]
            if canceled_at_ts
            else None
        ),
        created_at=existing.created_at if existing else datetime.now(UTC),
    )

    await repos.subscriptions.save(subscription)


async def _on_subscription_deleted(sub_data: dict[str, object], repos: WebhookRepos) -> None:
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


async def _on_invoice_paid(invoice_data: dict[str, object]) -> None:
    # TODO: persist Invoice record, send receipt email via Celery task
    logger.info("Invoice paid: %s", invoice_data.get("id"))


async def _on_invoice_failed(invoice_data: dict[str, object]) -> None:
    # TODO: trigger dunning email via Celery task
    # Subscription status (past_due / unpaid) is synced via the
    # customer.subscription.updated event Stripe fires alongside this one.
    logger.warning("Invoice payment failed: %s", invoice_data.get("id"))
