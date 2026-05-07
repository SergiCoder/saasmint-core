"""Tests for services/currency.py — all branches covered."""

from __future__ import annotations

from saasmint_core.services.currency import (
    SUPPORTED_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
    format_amount,
    round_friendly,
)

# ── Constants ─────────────────────────────────────────────────────────────────


def test_supported_currencies_count() -> None:
    assert len(SUPPORTED_CURRENCIES) == 20


def test_zero_decimal_currencies() -> None:
    assert "jpy" in ZERO_DECIMAL_CURRENCIES
    assert "krw" in ZERO_DECIMAL_CURRENCIES
    assert "usd" not in ZERO_DECIMAL_CURRENCIES


# ── format_amount ─────────────────────────────────────────────────────────────


def test_format_amount_regular_currency() -> None:
    assert format_amount(1999, "usd") == 19.99


def test_format_amount_regular_currency_uppercase() -> None:
    assert format_amount(500, "EUR") == 5.0


def test_format_amount_zero_decimal_jpy() -> None:
    assert format_amount(1500, "jpy") == 1500.0


def test_format_amount_zero_decimal_krw() -> None:
    assert format_amount(10000, "krw") == 10000.0


def test_format_amount_zero_decimal_uppercase() -> None:
    assert format_amount(500, "JPY") == 500.0


# ── round_friendly ───────────────────────────────────────────────────────────


def test_round_friendly_rounds_down_across_whole() -> None:
    # 9.09 → nearest of {8.99, 9.49, 9.99}: 8.99 wins (distance 0.10)
    assert round_friendly(9.09, "eur") == 8.99


def test_round_friendly_rounds_down_to_49() -> None:
    # 4.55 → nearest of {3.99, 4.49, 4.99}: 4.49 wins (distance 0.06)
    assert round_friendly(4.55, "eur") == 4.49


def test_round_friendly_rounds_up_near_whole() -> None:
    # 7.80 → nearest of {6.99, 7.49, 7.99}: 7.99 wins (distance 0.19)
    assert round_friendly(7.80, "gbp") == 7.99


def test_round_friendly_rounds_down_across_next_whole() -> None:
    # 17.23 → nearest of {16.99, 17.49, 17.99}: 16.99 wins (distance 0.24)
    assert round_friendly(17.23, "eur") == 16.99


def test_round_friendly_rounds_up_to_next_99() -> None:
    # 19.77 → nearest of {18.99, 19.49, 19.99}: 19.99 wins (distance 0.22)
    assert round_friendly(19.77, "eur") == 19.99


def test_round_friendly_zero_decimal_large() -> None:
    # ≥1000 → nearest 100
    assert round_friendly(149349.0, "jpy") == 149300.0


def test_round_friendly_zero_decimal_small() -> None:
    # <1000 → nearest 10
    assert round_friendly(454.0, "krw") == 450.0


def test_round_friendly_zero_decimal_exact_boundary() -> None:
    # Exactly on step boundary → stays unchanged
    assert round_friendly(1500.0, "jpy") == 1500.0


def test_round_friendly_exact_99_unchanged() -> None:
    assert round_friendly(9.99, "eur") == 9.99


def test_round_friendly_zero_amount() -> None:
    assert round_friendly(0.0, "eur") == 0.0


def test_round_friendly_whole_number_rounds_down() -> None:
    # 5.0 → nearest of {4.99, 5.49, 5.99}: 4.99 wins (distance 0.01)
    assert round_friendly(5.0, "eur") == 4.99
