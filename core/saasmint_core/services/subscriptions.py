"""Subscription lifecycle — plan upgrades/downgrades, seat changes."""

from __future__ import annotations

import asyncio
from typing import Literal

import stripe
from stripe.params._subscription_modify_params import (
    SubscriptionModifyParamsItem,
)


async def _get_first_item_id(stripe_subscription_id: str) -> str:
    """Retrieve a subscription and return its first line-item ID."""
    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    return str(sub["items"]["data"][0]["id"])


async def change_plan(
    *,
    stripe_subscription_id: str,
    new_stripe_price_id: str,
    prorate: bool = True,
    quantity: int | None = None,
) -> None:
    """
    Upgrade or downgrade to a new plan price, optionally updating quantity.

    When *quantity* is provided the plan switch and seat-count update are
    applied in a single Stripe API call, avoiding partial-update states.

    Proration is enabled by default: the customer is credited for unused time
    on the old plan and charged for the new plan immediately. Set prorate=False
    to switch at the next billing cycle with no immediate charge.

    DB state is synced via customer.subscription.updated webhook.
    """
    item_id = await _get_first_item_id(stripe_subscription_id)
    proration: Literal["create_prorations", "none"] = "create_prorations" if prorate else "none"

    item: SubscriptionModifyParamsItem = {"id": item_id, "price": new_stripe_price_id}
    if quantity is not None:
        item["quantity"] = quantity

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[item],
        proration_behavior=proration,
    )


async def update_seat_count(
    *,
    stripe_subscription_id: str,
    quantity: int,
) -> None:
    """
    Update the seat count for an org subscription.

    Adding seats prorates immediately (the org is charged for the new seat
    right away).  Removing seats applies at renewal — no mid-cycle credit.
    DB state is synced via customer.subscription.updated webhook.
    """
    if quantity < 1:
        raise ValueError("Seat count must be at least 1")

    item_id = await _get_first_item_id(stripe_subscription_id)

    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    current_quantity: int = sub["items"]["data"][0]["quantity"]
    proration: Literal["create_prorations", "none"] = (
        "create_prorations" if quantity > current_quantity else "none"
    )

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[{"id": item_id, "quantity": quantity}],
        proration_behavior=proration,
    )
