"""Tests for services/coupons.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from saasmint_core.exceptions import InvalidPromoCodeError
from saasmint_core.services.coupons import describe_discount, validate_promo_code

# ── validate_promo_code ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_validate_promo_code_empty_results_raises() -> None:
    mock_list = MagicMock()
    mock_list.data = []
    with patch("stripe.PromotionCode.list", return_value=mock_list):
        with pytest.raises(InvalidPromoCodeError, match="invalid or expired"):
            await validate_promo_code("BADCODE")


@pytest.mark.anyio
async def test_validate_promo_code_invalid_coupon_raises() -> None:
    mock_coupon = MagicMock()
    mock_coupon.valid = False
    mock_promo = MagicMock()
    mock_promo.coupon = mock_coupon
    mock_list = MagicMock()
    mock_list.data = [mock_promo]
    with patch("stripe.PromotionCode.list", return_value=mock_list):
        with pytest.raises(InvalidPromoCodeError, match="no longer valid"):
            await validate_promo_code("EXPIRED")


@pytest.mark.anyio
async def test_validate_promo_code_valid_returns_promo() -> None:
    mock_coupon = MagicMock()
    mock_coupon.valid = True
    mock_promo = MagicMock()
    mock_promo.coupon = mock_coupon
    mock_list = MagicMock()
    mock_list.data = [mock_promo]
    with patch("stripe.PromotionCode.list", return_value=mock_list):
        result = await validate_promo_code("SAVE20")
    assert result is mock_promo


# ── describe_discount ─────────────────────────────────────────────────────────


def _make_promo(
    percent_off: float | None = None,
    amount_off: int | None = None,
    currency: str | None = None,
    duration: str = "forever",
    duration_in_months: int | None = None,
) -> MagicMock:
    coupon = MagicMock()
    coupon.percent_off = percent_off
    coupon.amount_off = amount_off
    coupon.currency = currency
    coupon.duration = duration
    coupon.duration_in_months = duration_in_months
    promo = MagicMock()
    promo.coupon = coupon
    return promo


def test_describe_discount_percent_off_forever() -> None:
    promo = _make_promo(percent_off=20.0, duration="forever")
    assert describe_discount(promo) == "20% off forever"


def test_describe_discount_percent_off_once() -> None:
    promo = _make_promo(percent_off=10.0, duration="once")
    assert describe_discount(promo) == "10% off once"


def test_describe_discount_percent_off_repeating_plural() -> None:
    promo = _make_promo(percent_off=15.0, duration="repeating", duration_in_months=3)
    assert describe_discount(promo) == "15% off for 3 months"


def test_describe_discount_percent_off_repeating_singular() -> None:
    promo = _make_promo(percent_off=5.0, duration="repeating", duration_in_months=1)
    assert describe_discount(promo) == "5% off for 1 month"


def test_describe_discount_percent_off_unknown_duration() -> None:
    promo = _make_promo(percent_off=25.0, duration="unknown_duration")
    assert describe_discount(promo) == "25% off"


def test_describe_discount_amount_off_with_currency() -> None:
    promo = _make_promo(amount_off=500, currency="usd", duration="once")
    assert describe_discount(promo) == "5.00 USD off once"


def test_describe_discount_amount_off_zero_decimal_currency() -> None:
    promo = _make_promo(amount_off=1000, currency="jpy", duration="once")
    assert describe_discount(promo) == "1000 JPY off once"


def test_describe_discount_neither_percent_nor_amount() -> None:
    promo = _make_promo(percent_off=None, amount_off=None, currency=None, duration="forever")
    assert describe_discount(promo) == "discount forever"


def test_describe_discount_amount_off_no_currency_falls_to_discount() -> None:
    # amount_off set but currency is falsy → "discount" label
    promo = _make_promo(percent_off=None, amount_off=500, currency=None, duration="once")
    assert describe_discount(promo) == "discount once"


def test_describe_discount_repeating_no_months() -> None:
    promo = _make_promo(percent_off=10.0, duration="repeating", duration_in_months=None)
    assert describe_discount(promo) == "10% off repeating"
