"""Coupon and promo code validation against Stripe."""

from __future__ import annotations

import asyncio
from typing import Any

import stripe

from saasmint_core.exceptions import InvalidPromoCodeError
from saasmint_core.services.currency import ZERO_DECIMAL_CURRENCIES


async def validate_promo_code(promo_code: str) -> stripe.PromotionCode:
    """
    Validate a Stripe PromotionCode by its human-readable code.

    Raises InvalidPromoCodeError if:
    - Code does not exist
    - Code is inactive or expired
    - Code has exceeded its max_redemptions
    """
    results = await asyncio.to_thread(
        stripe.PromotionCode.list, code=promo_code, active=True, limit=1
    )

    if not results.data:
        raise InvalidPromoCodeError(f"Promo code '{promo_code}' is invalid or expired.")

    promotion_code = results.data[0]

    coupon: Any = promotion_code.coupon  # type: ignore[attr-defined]  # Stripe stub gap
    if not coupon.valid:  # pyright: ignore[reportUnknownMemberType]
        raise InvalidPromoCodeError(f"Promo code '{promo_code}' is no longer valid.")

    return promotion_code


def _coupon_to_dict(promotion_code: stripe.PromotionCode) -> dict[str, Any]:
    """Extract coupon fields into a typed dict to avoid pyright Unknown propagation."""
    coupon: Any = promotion_code.coupon  # type: ignore[attr-defined]  # Stripe stub gap
    return {
        "percent_off": coupon.percent_off,  # pyright: ignore[reportUnknownMemberType]
        "amount_off": coupon.amount_off,  # pyright: ignore[reportUnknownMemberType]
        "currency": coupon.currency,  # pyright: ignore[reportUnknownMemberType]
        "duration": coupon.duration,  # pyright: ignore[reportUnknownMemberType]
        "duration_in_months": coupon.duration_in_months,  # pyright: ignore[reportUnknownMemberType]
    }


def describe_discount(promotion_code: stripe.PromotionCode) -> str:
    """Return a human-readable discount description for display in the UI."""
    c = _coupon_to_dict(promotion_code)
    percent_off: float | None = c["percent_off"]
    amount_off: int | None = c["amount_off"]
    currency: str | None = c["currency"]
    duration_str: str = c["duration"]

    if percent_off is not None:
        amount = f"{int(percent_off)}% off"
    elif amount_off is not None and currency is not None:
        if currency.lower() in ZERO_DECIMAL_CURRENCIES:
            amount = f"{amount_off} {currency.upper()} off"
        else:
            amount = f"{amount_off / 100:.2f} {currency.upper()} off"
    else:
        amount = "discount"

    match duration_str:
        case "forever":
            duration = "forever"
        case "once":
            duration = "once"
        case "repeating":
            months: int | None = c["duration_in_months"]
            if months is not None:
                duration = f"for {months} month{'s' if months != 1 else ''}"
            else:
                duration = "repeating"
        case _:
            duration = ""

    return f"{amount} {duration}".strip()
