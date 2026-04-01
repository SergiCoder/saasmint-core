"""Tests for services/subscriptions.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from saasmint_core.exceptions import InvalidPromoCodeError
from saasmint_core.services.subscriptions import apply_promo_code, change_plan, update_seat_count


def _mock_stripe_sub(item_id: str = "si_abc") -> MagicMock:
    item = MagicMock()
    item.__getitem__ = lambda self, key: item_id if key == "id" else None
    sub = MagicMock()
    sub.__getitem__ = lambda self, key: {"data": [item]} if key == "items" else None
    return sub


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
async def test_update_seat_count_valid() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_seat"}]}}[k]
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
async def test_update_seat_count_minimum_valid() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_min"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=1)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_min", "quantity": 1}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_update_seat_count_zero_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=0)


@pytest.mark.anyio
async def test_update_seat_count_negative_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(stripe_subscription_id="sub_abc", quantity=-3)


# ── apply_promo_code ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_apply_promo_code_valid() -> None:
    mock_promo = MagicMock()
    mock_promo.id = "promo_xyz"
    mock_coupon = MagicMock()
    mock_coupon.valid = True
    mock_promo.coupon = mock_coupon
    mock_list = MagicMock()
    mock_list.data = [mock_promo]

    with (
        patch("stripe.PromotionCode.list", return_value=mock_list),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await apply_promo_code(stripe_subscription_id="sub_abc", promo_code="SAVE10")

    mock_modify.assert_called_once_with(
        "sub_abc",
        discounts=[{"promotion_code": "promo_xyz"}],
    )


@pytest.mark.anyio
async def test_apply_promo_code_invalid_propagates_error() -> None:
    mock_list = MagicMock()
    mock_list.data = []

    with patch("stripe.PromotionCode.list", return_value=mock_list):
        with pytest.raises(InvalidPromoCodeError):
            await apply_promo_code(stripe_subscription_id="sub_abc", promo_code="BADCODE")
