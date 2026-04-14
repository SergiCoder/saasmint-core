"""Tests for the health_check endpoint."""

from __future__ import annotations

import pytest
from django.test.client import Client
from django.urls import reverse


@pytest.mark.django_db
class TestHealthCheck:
    def test_returns_200(self, client: Client) -> None:
        resp = client.get("/api/v1/health/")
        assert resp.status_code == 200

    def test_returns_ok_payload(self, client: Client) -> None:
        resp = client.get("/api/v1/health/")
        assert resp.json() == {"status": "ok"}

    def test_accessible_without_authentication(self, client: Client) -> None:
        # No auth credentials set — endpoint must still respond OK.
        resp = client.get("/api/v1/health/")
        assert resp.status_code == 200

    def test_reverse_url_resolves(self) -> None:
        assert reverse("health-check") == "/api/v1/health/"

    def test_response_is_json(self, client: Client) -> None:
        resp = client.get("/api/v1/health/")
        assert resp["Content-Type"].startswith("application/json")
