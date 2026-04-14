"""Tests for reference-data endpoints (locales, currencies, phone-prefixes, timezones)."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

# Relax throttling in tests
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "references": "1000/hour",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


@pytest.fixture
def client():
    return APIClient()


class TestLocaleListView:
    def test_returns_sorted_locales(self, client):
        resp = client.get("/api/v1/locales/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert data == sorted(data)

    def test_contains_common_locales(self, client):
        resp = client.get("/api/v1/locales/")
        data = resp.json()
        assert "en" in data
        assert "es" in data

    def test_no_auth_required(self, client):
        """Reference endpoints allow unauthenticated access."""
        resp = client.get("/api/v1/locales/")
        assert resp.status_code == 200


class TestCurrencyListView:
    def test_returns_sorted_currencies(self, client):
        resp = client.get("/api/v1/currencies/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert data == sorted(data)

    def test_contains_common_currencies(self, client):
        resp = client.get("/api/v1/currencies/")
        data = resp.json()
        assert "usd" in data
        assert "eur" in data

    def test_no_auth_required(self, client):
        resp = client.get("/api/v1/currencies/")
        assert resp.status_code == 200


class TestPhonePrefixListView:
    def test_returns_list_of_prefix_objects(self, client):
        resp = client.get("/api/v1/phone-prefixes/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        first = data[0]
        assert "prefix" in first
        assert "label" in first

    def test_prefixes_start_with_plus(self, client):
        resp = client.get("/api/v1/phone-prefixes/")
        data = resp.json()
        for item in data:
            assert item["prefix"].startswith("+")

    def test_contains_us_prefix(self, client):
        resp = client.get("/api/v1/phone-prefixes/")
        data = resp.json()
        prefixes = [item["prefix"] for item in data]
        assert "+1" in prefixes

    def test_sorted_by_numeric_prefix(self, client):
        resp = client.get("/api/v1/phone-prefixes/")
        data = resp.json()
        numeric_values = [int(item["prefix"].lstrip("+")) for item in data]
        assert numeric_values == sorted(numeric_values)

    def test_no_auth_required(self, client):
        resp = client.get("/api/v1/phone-prefixes/")
        assert resp.status_code == 200


class TestTimezoneListView:
    def test_returns_sorted_timezones(self, client):
        resp = client.get("/api/v1/timezones/")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert data == sorted(data)

    def test_contains_common_timezones(self, client):
        resp = client.get("/api/v1/timezones/")
        data = resp.json()
        assert "UTC" in data
        assert "Europe/London" in data

    def test_no_auth_required(self, client):
        resp = client.get("/api/v1/timezones/")
        assert resp.status_code == 200
