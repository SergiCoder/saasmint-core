"""Subscription lifecycle — plan upgrades/downgrades, seat changes."""

from __future__ import annotations

import asyncio
from typing import Literal

import stripe
from stripe.params._subscription_modify_params import (
    SubscriptionModifyParamsItem,
)

from saasmint_core.domain.subscription import Subscription
from saasmint_core.repositories.subscription import SubscriptionRepository

# Returned by ``change_plan`` to tell the caller whether the switch happened
# now (immediate Subscription.modify) or was deferred to period end via a
# SubscriptionSchedule. The caller uses this to decide whether to skip the
# refetch (the scheduled mirror lands via webhook, not immediately) and
# what notice copy to surface.
ChangePlanResult = Literal["applied_now", "scheduled_for_period_end"]


def _safe_get(obj: object, key: str) -> object:
    """Return ``obj[key]`` or ``None`` if missing.

    ``stripe.StripeObject`` instances support ``__getitem__`` but not ``.get``
    — calling ``.get`` triggers ``__getattr__`` which raises ``AttributeError``
    instead of returning a default. Plain dicts also work via this helper.
    """
    if obj is None:
        return None
    try:
        return obj[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return None


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
    raw_quantity = _safe_get(first_item, "quantity")
    current_quantity = int(raw_quantity) if isinstance(raw_quantity, int) else 1

    # Only inspect ``price.unit_amount`` when the caller actually opted into
    # downgrade detection. Legacy callers pass dicts like {"id": ...} with no
    # ``price`` key — keep that path KeyError-free.
    is_downgrade = False
    if new_price_amount is not None:
        price_obj = _safe_get(first_item, "price")
        current_amount = _safe_get(price_obj, "unit_amount") if price_obj is not None else None
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

    # Immediate path (upgrade or same-amount switch). Stripe rejects
    # ``Subscription.modify`` when a SubscriptionSchedule owns the sub — the
    # schedule must be released first. This covers the case where the user had
    # previously scheduled a downgrade and now wants to upgrade instead.
    existing_schedule_id = _safe_get(sub, "schedule")
    if existing_schedule_id:
        await asyncio.to_thread(
            stripe.SubscriptionSchedule.release, str(existing_schedule_id)
        )
        # Re-fetch the sub so the items/item_id reflect the released state.
        sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
        first_item = sub["items"]["data"][0]
        item_id = str(first_item["id"])

    proration: Literal["create_prorations", "none"] = "create_prorations" if prorate else "none"

    # Always carry the current seat count forward. Stripe's Subscription.modify
    # treats a missing ``quantity`` on an item update as 1 — silently wiping
    # the seats on a plan switch when the caller didn't pass one explicitly.
    effective_quantity = quantity if quantity is not None else current_quantity
    item: SubscriptionModifyParamsItem = {
        "id": item_id,
        "price": new_stripe_price_id,
        "quantity": effective_quantity,
    }

    await asyncio.to_thread(
        stripe.Subscription.modify,
        stripe_subscription_id,
        items=[item],
        proration_behavior=proration,
    )
    return "applied_now"


def _read_period_field(
    sub: stripe.Subscription,
    first_item: object,
    field: str,
) -> int:
    """Read a period timestamp from the item first, then the subscription level.

    Stripe API 2024-06+ moved ``current_period_start`` / ``current_period_end``
    from the subscription object onto the subscription items. Older API versions
    (and some test fixtures) still place them at the subscription level, so we
    fall back there when the item has no value.

    Raises :class:`ValueError` when neither source provides an integer.
    """
    value = _safe_get(first_item, field)
    if value is None:
        value = _safe_get(sub, field)
    if not isinstance(value, int):
        raise ValueError(
            f"Subscription {sub['id']} missing integer {field}; "
            "cannot schedule a deferred downgrade"
        )
    return value


async def _schedule_downgrade_at_period_end(
    *,
    sub: stripe.Subscription,
    new_stripe_price_id: str,
    quantity: int,
) -> None:
    """Create (or update) a two-phase SubscriptionSchedule that swaps prices at period end.

    Phase 1 mirrors the current state (same price, same quantity) and ends
    at ``current_period_end``. Phase 2 starts at the same instant on the
    new price.

    When no schedule exists yet, Stripe requires the subscription to first be
    promoted to a schedule via ``from_subscription`` — that returns a one-phase
    schedule matching the current state, which we then ``modify`` to append
    phase 2.

    When a schedule already pins the sub (e.g. the user is revising a previously
    scheduled downgrade), we skip ``SubscriptionSchedule.create`` and go
    straight to ``SubscriptionSchedule.modify`` on the existing schedule.
    Stripe rejects a second ``create(from_subscription=...)`` call when the
    subscription is already managed by a schedule.
    """
    first_item = sub["items"]["data"][0]
    period_end = _read_period_field(sub, first_item, "current_period_end")
    period_start = _read_period_field(sub, first_item, "current_period_start")

    current_price_id = str(first_item["price"]["id"])

    # Defensive: Stripe rejects a SubscriptionSchedule whose phases mix
    # currencies. The view-layer guard in apps/billing/views.py
    # (_resolve_plan_change_price) already prevents this, but assert here
    # so a future caller can't accidentally bypass it.
    current_currency = str(first_item["price"].get("currency") or "").lower()
    new_price = await asyncio.to_thread(stripe.Price.retrieve, new_stripe_price_id)
    new_currency = str(new_price.currency or "").lower()
    if current_currency and new_currency and current_currency != new_currency:
        raise ValueError(
            f"Cannot schedule downgrade: subscription is in {current_currency.upper()} "
            f"but new price is in {new_currency.upper()}. "
            "Stripe pins subscription currency for life."
        )

    existing_schedule_id = _safe_get(sub, "schedule")
    if existing_schedule_id:
        # Sub is already managed by a schedule — modify it in place.
        schedule_id = str(existing_schedule_id)
    else:
        schedule = await asyncio.to_thread(
            stripe.SubscriptionSchedule.create,
            from_subscription=str(sub["id"]),
        )
        schedule_id = schedule["id"]

    await asyncio.to_thread(
        stripe.SubscriptionSchedule.modify,
        schedule_id,
        end_behavior="release",
        phases=[
            {
                "items": [
                    {"price": current_price_id, "quantity": quantity},
                ],
                "start_date": period_start,
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
    active: Subscription,
    quantity: int,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Update the seat count for an org subscription.

    Adding seats prorates immediately (the org is charged for the new seat
    right away). Removing seats updates Stripe immediately too — only the
    *billing impact* is deferred (``proration_behavior=none`` suppresses
    the credit).

    The new ``seat_limit`` is mirrored into the local row before returning
    so the frontend's revalidate-and-refetch sees the new value without
    waiting for the asynchronous ``customer.subscription.updated`` webhook.
    The webhook arrives later and re-saves the same value idempotently.
    """
    if quantity < 1:
        raise ValueError("Seat count must be at least 1")
    if active.stripe_id is None:
        raise ValueError("Subscription has no stripe_id; cannot update seat count")

    stripe_subscription_id = active.stripe_id
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

    if active.seat_limit != quantity:
        await subscription_repo.save(active.model_copy(update={"seat_limit": quantity}))
