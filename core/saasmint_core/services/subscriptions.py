"""Subscription lifecycle — plan upgrades/downgrades, seat changes."""

from __future__ import annotations

import asyncio
from typing import Literal

import stripe
from stripe.params._subscription_modify_params import (
    SubscriptionModifyParamsItem,
)

# Returned by ``change_plan`` to tell the caller whether the switch happened
# now (immediate Subscription.modify) or was deferred to period end via a
# SubscriptionSchedule. The caller uses this to decide whether to skip the
# refetch (the scheduled mirror lands via webhook, not immediately) and
# what notice copy to surface.
ChangePlanResult = Literal["applied_now", "scheduled_for_period_end"]


async def change_plan(
    *,
    stripe_subscription_id: str,
    new_stripe_price_id: str,
    new_price_amount: int | None = None,
    prorate: bool = True,
    quantity: int | None = None,
) -> ChangePlanResult:
    """
    Upgrade or downgrade to a new plan price, optionally updating quantity.

    When ``new_price_amount`` is provided (in the same minor units as the
    current price — USD cents in our catalog), the function compares it to
    the current item's ``unit_amount`` and **defers downgrades to the end
    of the billing period** by creating a Stripe ``SubscriptionSchedule``
    (current price phase → period end → new price phase). Upgrades and
    same-amount switches still apply immediately via ``Subscription.modify``
    so the customer pays the prorated difference up front.

    Omitting ``new_price_amount`` falls back to the legacy immediate-modify
    behavior — used by callers that don't need defer-on-downgrade semantics.

    When *quantity* is provided the plan switch and seat-count update are
    applied in a single Stripe API call, avoiding partial-update states.
    Quantity is preserved on the deferred-downgrade path too: the second
    schedule phase carries it forward so seats don't silently reset to 1.

    DB state for the immediate path is synced via
    ``customer.subscription.updated``; for the deferred path the
    ``subscription_schedule.created`` webhook mirrors the pending change
    onto the local row.

    Returns:
        ``"applied_now"`` for immediate ``Subscription.modify`` calls,
        ``"scheduled_for_period_end"`` when a SubscriptionSchedule was
        created.
    """
    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    first_item = sub["items"]["data"][0]
    item_id = str(first_item["id"])
    current_quantity = int(first_item.get("quantity") or 1)

    # Only inspect ``price.unit_amount`` when the caller actually opted into
    # downgrade detection. Legacy callers pass dicts like {"id": ...} with no
    # ``price`` key — keep that path KeyError-free.
    is_downgrade = False
    if new_price_amount is not None:
        price_obj = first_item.get("price")
        current_amount = (
            price_obj.get("unit_amount") if isinstance(price_obj, dict) else None
        )
        is_downgrade = (
            isinstance(current_amount, int) and new_price_amount < current_amount
        )

    if is_downgrade:
        # Subscription Schedules don't mix with subscriptions that already
        # carry a manual ``cancel_at`` — releasing isn't necessary because
        # ``cancel_at`` only kills the sub at period end and a downgrade
        # would never see phase 2 anyway. We still defer to schedule creation
        # because Stripe rejects schedules on canceled subs and the caller
        # is expected to have already validated the sub is active.
        await _schedule_downgrade_at_period_end(
            sub=sub,
            new_stripe_price_id=new_stripe_price_id,
            quantity=quantity if quantity is not None else current_quantity,
        )
        return "scheduled_for_period_end"

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
    return "applied_now"


async def _schedule_downgrade_at_period_end(
    *,
    sub: stripe.Subscription,
    new_stripe_price_id: str,
    quantity: int,
) -> None:
    """Create a two-phase SubscriptionSchedule that swaps prices at period end.

    Phase 1 mirrors the current state (same price, same quantity) and ends
    at ``current_period_end``. Phase 2 starts at the same instant on the
    new price. Stripe requires the subscription to first be promoted to a
    schedule via ``from_subscription`` — that returns a one-phase schedule
    matching the current state, which we then ``modify`` to append phase 2.
    """
    first_item = sub["items"]["data"][0]
    period_end = first_item.get("current_period_end")
    if period_end is None:
        # Older Stripe API versions placed period bounds at the subscription
        # level rather than the item. ``stripe.Subscription`` is subscriptable
        # at runtime even though the stub doesn't declare ``__getitem__`` for
        # the period field — fall back via dict access.
        period_end = sub.get("current_period_end")  # type: ignore[attr-defined]
    if not isinstance(period_end, int):
        raise ValueError(
            f"Subscription {sub['id']} missing integer current_period_end; "
            "cannot schedule a deferred downgrade"
        )

    current_price_id = str(first_item["price"]["id"])

    schedule = await asyncio.to_thread(
        stripe.SubscriptionSchedule.create,
        from_subscription=str(sub["id"]),
    )
    await asyncio.to_thread(
        stripe.SubscriptionSchedule.modify,
        schedule["id"],
        end_behavior="release",
        phases=[
            {
                "items": [
                    {"price": current_price_id, "quantity": quantity},
                ],
                "start_date": int(first_item["current_period_start"]),
                "end_date": period_end,
                "proration_behavior": "none",
            },
            {
                "items": [
                    {"price": new_stripe_price_id, "quantity": quantity},
                ],
                "start_date": period_end,
                "proration_behavior": "none",
            },
        ],
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

    # Single retrieve — read both item_id and current quantity from one Stripe
    # round-trip instead of calling `Subscription.retrieve` twice.
    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    first_item = sub["items"]["data"][0]
    item_id = str(first_item["id"])
    current_quantity: int = first_item["quantity"]
    proration: Literal["create_prorations", "none"] = (
        "create_prorations" if quantity > current_quantity else "none"
    )

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[{"id": item_id, "quantity": quantity}],
        proration_behavior=proration,
    )
