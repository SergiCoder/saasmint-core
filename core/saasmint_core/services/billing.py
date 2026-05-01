"""Core billing service — Stripe customer management, checkout, and cancellation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import stripe

from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.subscription import Subscription, SubscriptionStatus
from saasmint_core.exceptions import SubscriptionNotFoundError
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.subscription import SubscriptionRepository


async def get_or_create_customer(
    *,
    user_id: UUID | None = None,
    org_id: UUID | None = None,
    email: str,
    name: str | None = None,
    locale: str = "en",
    customer_repo: StripeCustomerRepository,
) -> StripeCustomer:
    """Return the existing Stripe customer for this user/org, or create one."""
    if user_id is not None:
        existing = await customer_repo.get_by_user_id(user_id)
    elif org_id is not None:
        existing = await customer_repo.get_by_org_id(org_id)
    else:
        raise ValueError("Either user_id or org_id must be provided")

    if existing:
        return existing

    metadata: dict[str, str] = {}
    if user_id is not None:
        metadata["user_id"] = str(user_id)
    if org_id is not None:
        metadata["org_id"] = str(org_id)

    stripe_customer = await asyncio.to_thread(
        stripe.Customer.create,
        email=email,
        name=name,  # type: ignore[arg-type]  # Stripe stub declares str, API accepts str | None
        preferred_locales=[locale],
        metadata=metadata,
    )

    return await customer_repo.save(
        StripeCustomer(
            id=uuid4(),
            stripe_id=stripe_customer.id,
            user_id=user_id,
            org_id=org_id,
            livemode=stripe_customer.livemode,
            created_at=datetime.now(UTC),
        )
    )


async def create_team_stripe_customer(
    *,
    user_id: UUID,
    email: str,
    name: str | None = None,
    locale: str = "en",
) -> str:
    """Create a fresh Stripe customer for a team checkout. No DB row.

    Team subscriptions belong to the org's Stripe customer; personal
    subscriptions belong to the user's. They must be different Stripe
    customers so each can keep its own locked currency and payment method
    (rule 3). For team checkout we therefore always mint a fresh Stripe
    customer here and let the ``checkout.session.completed`` webhook persist
    the matching ``StripeCustomer`` row scoped to the new org. The user-
    scoped customer (if any) is left untouched on its personal subscription.
    """
    stripe_customer = await asyncio.to_thread(
        stripe.Customer.create,
        email=email,
        name=name,  # type: ignore[arg-type]  # Stripe stub declares str, API accepts str | None
        preferred_locales=[locale],
        metadata={"user_id": str(user_id), "scope": "team_checkout"},
    )
    return stripe_customer.id


async def create_checkout_session(
    *,
    stripe_customer_id: str,
    price_id: str,
    client_reference_id: str,
    quantity: int = 1,
    locale: str = "en",
    success_url: str,
    cancel_url: str,
    trial_period_days: int | None = None,
    metadata: dict[str, str] | None = None,
) -> str:
    """Create a Stripe Checkout Session and return the hosted URL."""
    subscription_data: dict[str, object] = {}
    if trial_period_days is not None:
        subscription_data["trial_period_days"] = trial_period_days

    params: dict[str, object] = {
        "customer": stripe_customer_id,
        "client_reference_id": client_reference_id,
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": quantity}],
        "locale": locale,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }

    params["allow_promotion_codes"] = True
    params["adaptive_pricing"] = {"enabled": True}

    # Session-level metadata carries org fields for checkout.session.completed
    if metadata is not None:
        params["metadata"] = metadata

    if subscription_data:
        params["subscription_data"] = subscription_data

    session = await asyncio.to_thread(stripe.checkout.Session.create, **params)  # type: ignore[arg-type]  # Stripe stub can't validate **kwargs shape
    return session.url  # type: ignore[return-value]  # Stripe stub types url as str | None but hosted checkout always returns str


async def create_product_checkout_session(
    *,
    stripe_customer_id: str,
    price_id: str,
    client_reference_id: str,
    locale: str = "en",
    success_url: str,
    cancel_url: str,
    metadata: dict[str, str] | None = None,
) -> str:
    """Create a Stripe Checkout Session for a one-time product purchase.

    Uses ``mode=payment`` rather than ``mode=subscription``, so there's no
    ``subscription_data``/trial applicable. ``metadata`` is carried through to
    ``checkout.session.completed`` so the webhook can grant credits to the
    right owner (user or org).
    """
    params: dict[str, object] = {
        "customer": stripe_customer_id,
        "client_reference_id": client_reference_id,
        "mode": "payment",
        "line_items": [{"price": price_id, "quantity": 1}],
        "locale": locale,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "allow_promotion_codes": True,
        "adaptive_pricing": {"enabled": True},
    }
    if metadata is not None:
        params["metadata"] = metadata

    session = await asyncio.to_thread(stripe.checkout.Session.create, **params)  # type: ignore[arg-type]  # Stripe stub can't validate **kwargs shape
    return session.url  # type: ignore[return-value]  # Stripe stub types url as str | None but hosted checkout always returns str


async def create_billing_portal_session(
    *,
    stripe_customer_id: str,
    locale: str = "en",
    return_url: str,
    flow_data: dict[str, Any] | None = None,
) -> str:
    """Create a Stripe Customer Portal session and return the URL.

    When ``flow_data`` is provided, the portal deep-links into a focused
    flow (e.g. ``subscription_update_confirm``) instead of the home page.
    """
    params: dict[str, Any] = {
        "customer": stripe_customer_id,
        "locale": locale,
        "return_url": return_url,
    }
    if flow_data is not None:
        params["flow_data"] = flow_data
    session = await asyncio.to_thread(
        stripe.billing_portal.Session.create,
        **params,
    )
    return session.url


async def cancel_subscription(
    *,
    stripe_customer_id: UUID,
    at_period_end: bool = True,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Cancel the active subscription for a Stripe customer.

    When at_period_end=True the subscription stays active until the billing
    period ends (default — least disruptive). Set False for immediate cancellation
    (e.g. GDPR deletion).

    The Stripe response is mirrored into the local row before returning so the
    PATCH-then-GET path the frontend uses sees the new `cancel_at` immediately
    instead of waiting for the (asynchronous) ``customer.subscription.updated``
    webhook. The webhook still arrives later and re-saves the same row
    idempotently.
    """
    active = await subscription_repo.get_active_for_customer(stripe_customer_id)
    if active is None or active.stripe_id is None:
        raise SubscriptionNotFoundError("No active subscription found to cancel.")

    if at_period_end:
        # 2026-03-25.dahlia replaces `cancel_at_period_end=True` with
        # `cancel_at="min_period_end"`. For single-item subs (the only shape
        # we support) this is the direct equivalent.
        stripe_sub = await asyncio.to_thread(
            stripe.Subscription.modify, active.stripe_id, cancel_at="min_period_end"
        )
        await _mirror_cancel_state_from_stripe(active, stripe_sub, subscription_repo)
    else:
        stripe_sub = await asyncio.to_thread(stripe.Subscription.cancel, active.stripe_id)
        await _mirror_cancel_state_from_stripe(active, stripe_sub, subscription_repo)


async def resume_subscription(
    *,
    stripe_customer_id: UUID,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Clear a scheduled cancellation on the active subscription, keeping it open.

    Resumes a sub that was previously canceled with at_period_end=True (i.e.
    flagged with `cancel_at`). The sub must still be active — once it has
    fully ended, the customer must start a new checkout.

    Like :func:`cancel_subscription`, the Stripe response is mirrored into the
    local row before returning so the frontend sees ``cancel_at`` cleared
    without waiting for the webhook.
    """
    active = await subscription_repo.get_active_for_customer(stripe_customer_id)
    if active is None or active.stripe_id is None:
        raise SubscriptionNotFoundError("No active subscription found to resume.")

    # 2026-03-25.dahlia: clear a scheduled cancellation set via
    # cancel_at="min_period_end" by passing cancel_at="".
    stripe_sub = await asyncio.to_thread(stripe.Subscription.modify, active.stripe_id, cancel_at="")
    await _mirror_cancel_state_from_stripe(active, stripe_sub, subscription_repo)


async def _mirror_cancel_state_from_stripe(
    active: Subscription,
    stripe_sub: stripe.Subscription,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Persist ``cancel_at`` / ``canceled_at`` / ``status`` from a Stripe response.

    Closes the webhook race for cancel/resume: the frontend's revalidate-and-
    refetch lands before Stripe delivers ``customer.subscription.updated``, so
    without this sync the GET would still report the pre-cancel state. We only
    touch the cancel-state fields — `current_period_*`, `quantity`, etc. stay
    on whatever the webhook last wrote, since modify(cancel_at=...) doesn't
    change them.
    """
    cancel_at_ts = stripe_sub.cancel_at
    canceled_at_ts = stripe_sub.canceled_at
    status_str = stripe_sub.status

    update: dict[str, Any] = {
        "cancel_at": _ts_to_dt(cancel_at_ts),
        "canceled_at": _ts_to_dt(canceled_at_ts),
    }
    if isinstance(status_str, str):
        try:
            update["status"] = SubscriptionStatus(status_str)
        except ValueError:
            # Unknown status: leave the existing one rather than crashing the
            # mutation. The webhook will reconcile.
            pass

    await subscription_repo.save(active.model_copy(update=update))


def _ts_to_dt(value: int | float | None) -> datetime | None:
    """Stripe-style Unix timestamp → aware UTC datetime, ``None`` passthrough."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
