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
