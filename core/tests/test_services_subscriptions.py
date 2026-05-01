"""Tests for services/subscriptions.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from saasmint_core.services.subscriptions import change_plan, update_seat_count

# ── change_plan ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_change_plan_with_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_abc"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=True,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_abc", "price": "price_new"}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_change_plan_without_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_def"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=False,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_def", "price": "price_new"}],
        proration_behavior="none",
    )


@pytest.mark.anyio
async def test_change_plan_with_quantity() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_combo"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=True,
            quantity=5,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_combo", "price": "price_new", "quantity": 5}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_change_plan_with_quantity_no_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_nopro"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=False,
            quantity=3,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_nopro", "price": "price_new", "quantity": 3}],
        proration_behavior="none",
    )


# ── update_seat_count ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_update_seat_count_increase_prorates() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_seat", "quantity": 3}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=5)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_seat", "quantity": 5}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_update_seat_count_decrease_no_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_seat", "quantity": 5}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=3)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_seat", "quantity": 3}],
        proration_behavior="none",
    )


@pytest.mark.anyio
async def test_update_seat_count_minimum_valid() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_min", "quantity": 1}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=1)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_min", "quantity": 1}],
        proration_behavior="none",
    )


@pytest.mark.anyio
async def test_update_seat_count_zero_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=0)


@pytest.mark.anyio
async def test_update_seat_count_negative_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=-3)


# ── change_plan: defer-on-downgrade ──────────────────────────────────────────


def _stripe_sub_dict(
    *,
    sub_id: str = "sub_dg",
    item_id: str = "si_dg",
    price_id: str = "price_pro",
    unit_amount: int = 2000,
    quantity: int = 1,
    period_start: int = 1_700_000_000,
    period_end: int = 1_702_592_000,
) -> dict[str, object]:
    """Stripe-shaped subscription dict for change_plan tests.

    Carries ``unit_amount`` and ``current_period_start/end`` on the first
    item — the fields ``change_plan`` needs to decide whether to defer and
    to build the SubscriptionSchedule phases.
    """
    return {
        "id": sub_id,
        "items": {
            "data": [
                {
                    "id": item_id,
                    "price": {"id": price_id, "unit_amount": unit_amount},
                    "quantity": quantity,
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                }
            ]
        },
    }


@pytest.mark.anyio
async def test_change_plan_downgrade_defers_via_schedule() -> None:
    """``new_price_amount < current unit_amount`` → no Subscription.modify;
    a two-phase SubscriptionSchedule is created instead. Phase 0 keeps the
    current price until period end; phase 1 starts at period end on the
    new price. Both phases preserve the current quantity."""
    sub = _stripe_sub_dict(
        unit_amount=2000, quantity=3, period_start=1_700_000_000, period_end=1_702_592_000
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch(
            "stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_new"}
        ) as mock_create,
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
        )

    assert result == "scheduled_for_period_end"
    mock_modify.assert_not_called()
    mock_create.assert_called_once_with(from_subscription="sub_dg")
    args, kwargs = mock_sched_modify.call_args
    assert args[0] == "sub_sched_new"
    assert kwargs["end_behavior"] == "release"
    phases = kwargs["phases"]
    assert len(phases) == 2
    # Phase 0: stay on current price until period end, with current seat count.
    assert phases[0]["items"][0]["price"] == "price_pro"
    assert phases[0]["items"][0]["quantity"] == 3
    assert phases[0]["end_date"] == 1_702_592_000
    # Phase 1: switch to new price at the same instant; seats preserved.
    assert phases[1]["items"][0]["price"] == "price_basic"
    assert phases[1]["items"][0]["quantity"] == 3
    assert phases[1]["start_date"] == 1_702_592_000


@pytest.mark.anyio
async def test_change_plan_upgrade_applies_immediately() -> None:
    """``new_price_amount > current unit_amount`` → Subscription.modify (no
    schedule). Customer pays the prorated diff now."""
    sub = _stripe_sub_dict(unit_amount=999)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_pro",
            new_price_amount=2000,
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_same_amount_applies_immediately() -> None:
    """Same-amount switch (e.g. tier swap with identical price) is not a
    downgrade — no defer, immediate modify."""
    sub = _stripe_sub_dict(unit_amount=1500)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_other",
            new_price_amount=1500,
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_without_amount_uses_legacy_immediate_path() -> None:
    """Backwards compat: callers that don't pass ``new_price_amount`` get
    immediate-modify behavior regardless of whether the swap would
    otherwise be a downgrade. The Stripe portal deep-link callers + any
    legacy code path are not forced into schedule creation."""
    sub = _stripe_sub_dict(unit_amount=2000)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_downgrade_quantity_override_wins() -> None:
    """An explicit ``quantity`` argument overrides the current seat count
    on both phases — a deferred downgrade can also adjust seats."""
    sub = _stripe_sub_dict(unit_amount=2000, quantity=5)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch(
            "stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_q"}
        ),
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
            quantity=2,
        )

    phases = mock_sched_modify.call_args.kwargs["phases"]
    assert phases[0]["items"][0]["quantity"] == 2
    assert phases[1]["items"][0]["quantity"] == 2
