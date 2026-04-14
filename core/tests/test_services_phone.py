"""Tests for services/phone.py — sort_prefix_key and SUPPORTED_PHONE_PREFIXES."""

from __future__ import annotations

from saasmint_core.services.phone import SUPPORTED_PHONE_PREFIXES, sort_prefix_key


def test_sort_prefix_key_strips_plus_and_returns_int() -> None:
    assert sort_prefix_key(("+1", "US/CA")) == 1


def test_sort_prefix_key_multi_digit() -> None:
    assert sort_prefix_key(("+34", "ES")) == 34


def test_sort_prefix_key_three_digit() -> None:
    assert sort_prefix_key(("+351", "PT")) == 351


def test_supported_phone_prefixes_all_start_with_plus() -> None:
    for prefix in SUPPORTED_PHONE_PREFIXES:
        assert prefix.startswith("+"), f"Prefix {prefix!r} does not start with '+'"


def test_supported_phone_prefixes_all_numeric_after_plus() -> None:
    for prefix in SUPPORTED_PHONE_PREFIXES:
        assert prefix[1:].isdigit(), f"Prefix {prefix!r} has non-numeric chars after '+'"


def test_supported_phone_prefixes_values_are_nonempty_strings() -> None:
    for prefix, label in SUPPORTED_PHONE_PREFIXES.items():
        assert isinstance(label, str) and len(label) > 0, (
            f"Label for {prefix} must be a non-empty string"
        )


def test_supported_phone_prefixes_is_immutable() -> None:
    """SUPPORTED_PHONE_PREFIXES should be a MappingProxyType (immutable)."""
    from types import MappingProxyType

    assert isinstance(SUPPORTED_PHONE_PREFIXES, MappingProxyType)


def test_sort_prefix_key_produces_correct_ordering() -> None:
    """Sorting by sort_prefix_key should produce numeric ascending order."""
    items = list(SUPPORTED_PHONE_PREFIXES.items())
    sorted_items = sorted(items, key=sort_prefix_key)
    numeric_values = [int(k.lstrip("+")) for k, _ in sorted_items]
    assert numeric_values == sorted(numeric_values)
