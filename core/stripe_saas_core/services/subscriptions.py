"""Subscription lifecycle — plan upgrades/downgrades, seat changes, promo codes."""

from __future__ import annotations

import asyncio

import stripe

from stripe_saas_core.services.coupons import validate_promo_code


async def _get_first_item_id(stripe_subscription_id: str) -> str:
    """Retrieve a subscription and return its first line-item ID."""
    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    return str(sub["items"]["data"][0]["id"])


async def change_plan(
    *,
    stripe_subscription_id: str,
    new_stripe_price_id: str,
    prorate: bool = True,
) -> None:
    """
    Upgrade or downgrade to a new plan price.

    Proration is enabled by default: the customer is credited for unused time
    on the old plan and charged for the new plan immediately. Set prorate=False
    to switch at the next billing cycle with no immediate charge.

    DB state is synced via customer.subscription.updated webhook.
    """
    item_id = await _get_first_item_id(stripe_subscription_id)

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[{"id": item_id, "price": new_stripe_price_id}],
        proration_behavior="create_prorations" if prorate else "none",
    )


async def update_seat_count(
    *,
    stripe_subscription_id: str,
    quantity: int,
) -> None:
    """
    Update the seat count for an org subscription.

    Prorates immediately so the customer is billed/credited for the delta.
    DB state is synced via customer.subscription.updated webhook.
    """
    if quantity < 1:
        raise ValueError("Seat count must be at least 1")

    item_id = await _get_first_item_id(stripe_subscription_id)

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[{"id": item_id, "quantity": quantity}],
        proration_behavior="create_prorations",
    )


async def apply_promo_code(
    *,
    stripe_subscription_id: str,
    promo_code: str,
) -> None:
    """
    Validate and apply a promo code to an existing subscription.

    Raises InvalidPromoCodeError if the code is invalid or expired.
    DB state (discount_percent, discount_end_at, promotion_code_id) is synced
    via customer.subscription.updated webhook.
    """
    promo = await validate_promo_code(promo_code)
    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        discounts=[{"promotion_code": promo.id}],
    )
