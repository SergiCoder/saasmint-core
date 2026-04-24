"""Stripe webhook dispatch — operates on an already-persisted event."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import stripe

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
# Args: user_id, org_name, stripe_customer_id, livemode, stripe_subscription_id
OnTeamCheckoutCompleted = Callable[[UUID, str, str, bool, str | None], Awaitable[None]]

# Callback type for org deactivation after subscription cancellation.
# Args: org_id
OnOrgSubscriptionCanceled = Callable[[UUID], Awaitable[None]]

# Callback type for product (one-time) checkout completion.
# Args: stripe_session_id, product_id, user_id, org_id (None for personal buys)
OnProductCheckoutCompleted = Callable[[str, UUID, UUID, UUID | None], Awaitable[None]]


@dataclass(frozen=True)
class WebhookRepos:
    events: StripeEventRepository
    subscriptions: SubscriptionRepository
    customers: StripeCustomerRepository
    plans: PlanRepository
    on_team_checkout_completed: OnTeamCheckoutCompleted | None = field(default=None)
    on_org_subscription_canceled: OnOrgSubscriptionCanceled | None = field(default=None)
    on_product_checkout_completed: OnProductCheckoutCompleted | None = field(default=None)


async def process_stored_event(
    event: dict[str, Any],
    stripe_id: str,
    repos: WebhookRepos,
) -> None:
    """Dispatch a previously-persisted Stripe event.

    The event is assumed to have been signature-verified and saved by the
    webhook endpoint before enqueueing — this function only routes it and
    updates the processed/failed status.

    Raises:
        WebhookDataError: the event references entities the system can't
            resolve (unknown customer, price, missing fields). Caller must
            not retry — the error is permanent.
        stripe.StripeError, ConnectionError: transient errors from upstream
            calls during dispatch. Caller should retry.
    """
    from saasmint_core.exceptions import WebhookDataError

    try:
        await _dispatch(event, repos)
        await repos.events.mark_processed(stripe_id)
    except WebhookDataError as exc:
        await repos.events.mark_failed(stripe_id, str(exc))
        raise
    except (stripe.StripeError, ConnectionError) as exc:
        await repos.events.mark_failed(stripe_id, str(exc))
        raise
    except Exception as exc:
        await repos.events.mark_failed(stripe_id, str(exc))
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
    """Convert an optional Unix timestamp to a UTC datetime, or None."""
    return datetime.fromtimestamp(int(value), tz=UTC) if value is not None else None


async def _on_checkout_completed(session_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Handle checkout.session.completed — route by mode to the right handler.

    ``mode=payment`` sessions are one-time product purchases (credit packs);
    ``mode=subscription`` sessions are plan checkouts, where the team-checkout
    branch runs only when ``metadata.org_name`` is present.
    """
    if session_data.get("mode") == "payment":
        await _on_product_checkout_completed(session_data, repos)
        return

    metadata = session_data.get("metadata") or {}
    org_name = metadata.get("org_name")

    if not org_name:
        # Not a team checkout with org metadata — nothing to do
        logger.debug("checkout.session.completed without org metadata, skipping")
        return

    client_ref = session_data.get("client_reference_id")
    if not client_ref:
        logger.warning("checkout.session.completed missing client_reference_id")
        return

    user_id = UUID(client_ref)
    stripe_customer_id = session_data.get("customer")
    if not stripe_customer_id:
        logger.warning("checkout.session.completed missing customer")
        return
    subscription_id = session_data.get("subscription")

    livemode: bool = session_data.get("livemode", False)

    if repos.on_team_checkout_completed is not None:
        await repos.on_team_checkout_completed(
            user_id, org_name, str(stripe_customer_id), livemode, subscription_id
        )
    else:
        logger.warning(
            "Team checkout completed for user %s but no callback registered",
            user_id,
        )


async def _on_product_checkout_completed(session_data: dict[str, Any], repos: WebhookRepos) -> None:
    """Handle a mode=payment checkout session — grant credits for a product purchase."""
    session_id = session_data.get("id")
    if not session_id:
        logger.warning("product checkout.session.completed missing session id")
        return

    metadata = session_data.get("metadata") or {}
    product_ref = metadata.get("product_id")
    if not product_ref:
        logger.warning("product checkout session %s missing product_id metadata", session_id)
        return

    client_ref = session_data.get("client_reference_id")
    if not client_ref:
        logger.warning("product checkout session %s missing client_reference_id", session_id)
        return

    try:
        product_id = UUID(product_ref)
        user_id = UUID(client_ref)
    except ValueError:
        logger.warning("product checkout session %s has malformed id metadata", session_id)
        return

    org_ref = metadata.get("org_id")
    try:
        org_id = UUID(org_ref) if org_ref else None
    except ValueError:
        logger.warning("product checkout session %s has malformed org_id metadata", session_id)
        return

    if repos.on_product_checkout_completed is not None:
        await repos.on_product_checkout_completed(str(session_id), product_id, user_id, org_id)
    else:
        logger.warning(
            "Product checkout completed (session %s) but no callback registered",
            session_id,
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
    if not isinstance(period_start, int) or not isinstance(period_end, int):
        raise WebhookDataError(
            f"Subscription {stripe_sub_id} has non-integer current_period_start/end"
        )

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
        current_period_start=datetime.fromtimestamp(period_start, tz=UTC),
        current_period_end=datetime.fromtimestamp(period_end, tz=UTC),
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

    # Org subscriptions: deactivate the org so members lose access immediately.
    if existing.user_id is None:
        if existing.stripe_customer_id is None:
            return
        customer = await repos.customers.get_by_id(existing.stripe_customer_id)
        if customer is not None and customer.org_id is not None:
            if repos.on_org_subscription_canceled is not None:
                await repos.on_org_subscription_canceled(customer.org_id)
            else:
                logger.warning(
                    "Org subscription %s canceled but no deactivation callback registered",
                    stripe_sub_id,
                )
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
    logger.info("Invoice paid: %s", invoice_data.get("id"))


async def _on_invoice_failed(invoice_data: dict[str, Any]) -> None:
    # Subscription status (past_due / unpaid) is synced via the
    # customer.subscription.updated event Stripe fires alongside this one.
    logger.warning("Invoice payment failed: %s", invoice_data.get("id"))
