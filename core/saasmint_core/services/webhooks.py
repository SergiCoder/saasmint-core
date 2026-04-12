"""Idempotent Stripe webhook handler — stores every event before processing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import stripe

from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import (
    FREE_SUBSCRIPTION_PERIOD_END,
    Subscription,
    SubscriptionStatus,
)
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.plan import PlanRepository
from saasmint_core.repositories.stripe_event import StripeEventRepository
from saasmint_core.repositories.subscription import SubscriptionRepository

logger = logging.getLogger(__name__)

# Callback type for team checkout completion.
# Args: user_id, org_name, org_slug, stripe_subscription_id
OnTeamCheckoutCompleted = Callable[[UUID, str, str, str | None], Awaitable[None]]


@dataclass(frozen=True)
class WebhookRepos:
    events: StripeEventRepository
    subscriptions: SubscriptionRepository
    customers: StripeCustomerRepository
    plans: PlanRepository
    on_team_checkout_completed: OnTeamCheckoutCompleted | None = field(default=None)


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
        stripe_event = stripe.Webhook.construct_event(payload, signature, webhook_secret)  # type: ignore[no-untyped-call]  # Stripe stub missing return type annotation
    except stripe.SignatureVerificationError as exc:
        raise WebhookVerificationError("Invalid Stripe webhook signature") from exc

    # Convert StripeObject → plain nested dict at the boundary. Newer
    # stripe-python versions don't inherit StripeObject from dict, so it
    # has no `.get()` and `dict(event)` raises KeyError. `.to_dict()`
    # recurses into nested StripeObjects. Tests stub construct_event to
    # return a plain dict directly, so accept that case as-is.
    event: dict[str, Any] = (
        stripe_event.to_dict() if hasattr(stripe_event, "to_dict") else stripe_event
    )
    stripe_id: str = event["id"]

    is_new = await repos.events.save_if_new(
        StripeEvent(
            id=uuid4(),
            stripe_id=stripe_id,
            type=event["type"],
            livemode=event["livemode"],
            payload=event,
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


async def _dispatch(event: dict[str, Any], repos: WebhookRepos) -> None:
    match event["type"]:
        case "checkout.session.completed":
            await _on_checkout_completed(event["data"]["object"], repos)
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


def _ts_to_dt(value: int | float | None) -> datetime | None:
    """Convert an optional Unix timestamp to a UTC datetime."""
    return datetime.fromtimestamp(int(value), tz=UTC) if value is not None else None


def _ts_to_dt_required(value: int | float) -> datetime:
    """Convert a Unix timestamp to a UTC datetime (required field)."""
    return datetime.fromtimestamp(int(value), tz=UTC)


async def _on_checkout_completed(session_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Handle checkout.session.completed — create org for team plan checkouts."""
    metadata = session_data.get("metadata") or {}
    org_name = metadata.get("org_name")
    org_slug = metadata.get("org_slug")

    if not org_name or not org_slug:
        # Not a team checkout with org metadata — nothing to do
        logger.debug("checkout.session.completed without org metadata, skipping")
        return

    client_ref = session_data.get("client_reference_id")
    if not client_ref:
        logger.warning("checkout.session.completed missing client_reference_id")
        return

    user_id = UUID(client_ref)
    subscription_id = session_data.get("subscription")

    if repos.on_team_checkout_completed is not None:
        await repos.on_team_checkout_completed(user_id, org_name, org_slug, subscription_id)
    else:
        logger.warning(
            "Team checkout completed for user %s but no callback registered",
            user_id,
        )


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

    # Stripe API 2024-06+ moved current_period_start/end from the subscription
    # object to the subscription items. Read from the item first, fall back to
    # the top-level for older API versions / fixtures.
    period_start = first_item.get("current_period_start", sub_data.get("current_period_start"))
    period_end = first_item.get("current_period_end", sub_data.get("current_period_end"))
    if period_start is None or period_end is None:
        raise WebhookDataError(f"Subscription {stripe_sub_id} missing current_period_start/end")

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
    subscription = Subscription(
        id=existing.id if existing else uuid4(),
        stripe_id=stripe_sub_id,
        stripe_customer_id=customer.id,
        user_id=customer.user_id,  # None for org subs; mirrored so user-scoped queries work
        status=SubscriptionStatus(str(sub_data["status"])),
        plan_id=plan_price.plan_id,
        quantity=int(first_item.get("quantity") or 1),
        trial_ends_at=_ts_to_dt(sub_data.get("trial_end")),
        current_period_start=_ts_to_dt_required(period_start),
        current_period_end=_ts_to_dt_required(period_end),
        canceled_at=_ts_to_dt(sub_data.get("canceled_at")),
        created_at=existing.created_at if existing else datetime.now(UTC),
    )

    await repos.subscriptions.save(subscription)

    # Prune the placeholder free subscription for personal users whenever they
    # have a paid sub synced. Runs on every created/updated event (not just the
    # first) so that retries after a crash between save() and cleanup still
    # converge — delete_free_for_user is a filtered DELETE and idempotent.
    if customer.user_id is not None and not subscription.is_free:
        deleted = await repos.subscriptions.delete_free_for_user(customer.user_id)
        if deleted:
            logger.info(
                "Removed %d free subscription(s) for user %s after sync of %s",
                deleted,
                customer.user_id,
                stripe_sub_id,
            )


async def _on_subscription_deleted(sub_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Mark a subscription as canceled and auto-fallback personal users to free."""
    stripe_sub_id = str(sub_data["id"])
    existing = await repos.subscriptions.get_by_stripe_id(stripe_sub_id)
    if existing is None:
        logger.warning("Received deletion event for unknown subscription %s", stripe_sub_id)
        return

    now = datetime.now(UTC)
    canceled = existing.model_copy(
        update={"status": SubscriptionStatus.CANCELED, "canceled_at": now}
    )
    await repos.subscriptions.save(canceled)

    # Auto-fallback: personal users land back on the free plan so they keep
    # using the product with free-tier limits. Org subs are skipped — there's
    # no team-level free plan.
    if existing.user_id is None:
        return

    free_plan = await repos.plans.get_free_plan()
    if free_plan is None:
        logger.warning(
            "No free plan found; user %s has no active subscription after %s cancellation",
            existing.user_id,
            stripe_sub_id,
        )
        return

    await repos.subscriptions.save(
        Subscription(
            id=uuid4(),
            stripe_id=None,
            stripe_customer_id=None,
            user_id=existing.user_id,
            status=SubscriptionStatus.ACTIVE,
            plan_id=free_plan.id,
            quantity=1,
            current_period_start=now,
            current_period_end=FREE_SUBSCRIPTION_PERIOD_END,
            created_at=now,
        )
    )


async def _on_invoice_paid(invoice_data: dict[str, Any]) -> None:
    # TODO: persist Invoice record, send receipt email via Celery task
    logger.info("Invoice paid: %s", invoice_data.get("id"))


async def _on_invoice_failed(invoice_data: dict[str, Any]) -> None:
    # TODO: trigger dunning email via Celery task
    # Subscription status (past_due / unpaid) is synced via the
    # customer.subscription.updated event Stripe fires alongside this one.
    logger.warning("Invoice payment failed: %s", invoice_data.get("id"))
