"""Tests for services/currency.py — all branches covered."""

from __future__ import annotations

from stripe_saas_core.services.currency import (
    COUNTRY_CURRENCY_MAP,
    SUPPORTED_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
    _currency_from_accept_language,
    format_amount,
    resolve_currency,
)

# ── Constants ─────────────────────────────────────────────────────────────────


def test_supported_currencies_count() -> None:
    assert len(SUPPORTED_CURRENCIES) == 20


def test_zero_decimal_currencies() -> None:
    assert "jpy" in ZERO_DECIMAL_CURRENCIES
    assert "krw" in ZERO_DECIMAL_CURRENCIES
    assert "usd" not in ZERO_DECIMAL_CURRENCIES


def test_country_currency_map_has_entries() -> None:
    assert COUNTRY_CURRENCY_MAP["US"] == "usd"
    assert COUNTRY_CURRENCY_MAP["GB"] == "gbp"
    assert COUNTRY_CURRENCY_MAP["JP"] == "jpy"


# ── resolve_currency ──────────────────────────────────────────────────────────


def test_resolve_preferred_valid() -> None:
    assert resolve_currency(preferred="EUR") == "eur"


def test_resolve_preferred_already_lowercase() -> None:
    assert resolve_currency(preferred="gbp") == "gbp"


def test_resolve_preferred_unsupported_falls_through_to_country() -> None:
    # "xyz" not in SUPPORTED_CURRENCIES → falls through to billing_country
    assert resolve_currency(preferred="xyz", billing_country="JP") == "jpy"


def test_resolve_preferred_none_uses_billing_country() -> None:
    assert resolve_currency(billing_country="DE") == "eur"


def test_resolve_billing_country_unknown_falls_through() -> None:
    # ZZ not in COUNTRY_CURRENCY_MAP → falls through to accept_language
    assert resolve_currency(billing_country="ZZ", accept_language="fr-FR,fr;q=0.9") == "eur"


def test_resolve_accept_language() -> None:
    assert resolve_currency(accept_language="en-US,en;q=0.9") == "usd"


def test_resolve_accept_language_with_country_match() -> None:
    assert resolve_currency(accept_language="pt-BR;q=0.8") == "brl"


def test_resolve_all_none_defaults_to_usd() -> None:
    assert resolve_currency() == "usd"


def test_resolve_preferred_empty_string_falls_through() -> None:
    # empty string is falsy → skips preferred branch
    assert resolve_currency(preferred="", billing_country="CH") == "chf"


def test_resolve_accept_language_no_match_defaults_usd() -> None:
    # Accept-Language with no country we know → default usd
    assert resolve_currency(accept_language="esperanto") == "usd"


# ── _currency_from_accept_language ────────────────────────────────────────────


def test_accept_language_simple_tag_no_dash() -> None:
    # "en" has no dash → skipped
    assert _currency_from_accept_language("en") is None


def test_accept_language_tag_with_unknown_country() -> None:
    # "xx-ZZ" → ZZ not in COUNTRY_CURRENCY_MAP → None
    assert _currency_from_accept_language("xx-ZZ") is None


def test_accept_language_first_match_wins() -> None:
    result = _currency_from_accept_language("ja-JP,en-US;q=0.9")
    assert result == "jpy"


def test_accept_language_with_quality_values() -> None:
    result = _currency_from_accept_language("de-DE;q=0.8")
    assert result == "eur"


def test_accept_language_multiple_no_match_then_match() -> None:
    # First tag has no dash, second has a match
    result = _currency_from_accept_language("zz, ko-KR")
    assert result == "krw"


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
