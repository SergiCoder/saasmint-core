"""Tests for services/locale.py — SUPPORTED_LOCALES constant."""

from __future__ import annotations

from saasmint_core.services.locale import SUPPORTED_LOCALES


def test_supported_locales_count() -> None:
    assert len(SUPPORTED_LOCALES) == 20


def test_supported_locales_includes_english() -> None:
    assert "en" in SUPPORTED_LOCALES


def test_supported_locales_includes_arabic() -> None:
    assert "ar" in SUPPORTED_LOCALES


def test_supported_locales_includes_regional_variants() -> None:
    assert "pt-BR" in SUPPORTED_LOCALES
    assert "zh-CN" in SUPPORTED_LOCALES
    assert "zh-TW" in SUPPORTED_LOCALES
    assert "pt-PT" in SUPPORTED_LOCALES


def test_supported_locales_is_frozenset() -> None:
    assert isinstance(SUPPORTED_LOCALES, frozenset)
