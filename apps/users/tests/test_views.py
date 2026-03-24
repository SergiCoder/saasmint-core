"""Tests for AccountView and AccountExportView."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from rest_framework.test import APIClient

from apps.users.models import User

# Disable throttling in tests
_NO_THROTTLE = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {},
}


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="view@example.com",
        supabase_uid="sup_view",
        full_name="View User",
    )


@pytest.fixture
def authed_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _NO_THROTTLE


@pytest.mark.django_db
class TestAccountViewGET:
    def test_returns_current_user(self, authed_client, user):
        resp = authed_client.get("/api/v1/account/")
        assert resp.status_code == 200
        assert resp.data["email"] == user.email
        assert resp.data["full_name"] == "View User"

    def test_unauthenticated_returns_403(self):
        client = APIClient()
        resp = client.get("/api/v1/account/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountViewPATCH:
    def test_update_full_name(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"full_name": "Updated Name"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["full_name"] == "Updated Name"
        user.refresh_from_db()
        assert user.full_name == "Updated Name"

    def test_update_locale(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_locale": "es"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["preferred_locale"] == "es"

    def test_invalid_locale_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_locale": "invalid"},
            format="json",
        )
        assert resp.status_code == 400

    def test_invalid_currency_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_currency": "zzz"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestAccountViewDELETE:
    @patch("apps.users.views.delete_user_data", new_callable=AsyncMock)
    def test_delete_calls_gdpr_service(self, mock_delete, authed_client, user):
        resp = authed_client.delete("/api/v1/account/")
        assert resp.status_code == 204
        mock_delete.assert_called_once()
        call_kwargs = mock_delete.call_args.kwargs
        assert call_kwargs["user_id"] == user.id


@pytest.mark.django_db
class TestAccountExportView:
    @patch("apps.users.views.export_user_data", new_callable=AsyncMock)
    def test_export_returns_data(self, mock_export, authed_client, user):
        mock_export.return_value = {"user": {"email": user.email}}
        resp = authed_client.get("/api/v1/account/export/")
        assert resp.status_code == 200
        assert resp.data["user"]["email"] == user.email
        mock_export.assert_called_once()
