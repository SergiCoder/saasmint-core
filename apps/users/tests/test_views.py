"""Tests for AccountView and AccountExportView."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rest_framework.test import APIClient

from apps.users.models import User

# Relax throttling in tests — keep scoped rates so ScopedRateThrottle can resolve them
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "account": "1000/hour",
        "account_export": "1000/hour",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="view@example.com",
        full_name="View User",
    )


@pytest.fixture
def authed_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


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
class TestAccountViewPATCHEdgeCases:
    def test_update_multiple_fields_at_once(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"full_name": "Multi Update", "preferred_locale": "en", "preferred_currency": "eur"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["full_name"] == "Multi Update"
        assert resp.data["preferred_locale"] == "en"
        assert resp.data["preferred_currency"] == "eur"
        user.refresh_from_db()
        assert user.preferred_currency == "eur"

    def test_update_avatar_url(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"avatar_url": "https://cdn.example.com/img.png"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["avatar_url"] == "https://cdn.example.com/img.png"

    def test_update_empty_body_is_noop(self, authed_client, user):
        original_name = user.full_name
        resp = authed_client.patch(
            "/api/v1/account/",
            {},
            format="json",
        )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.full_name == original_name

    def test_update_phone(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": {"prefix": "+34", "number": "612345678"}},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["phone"] == {"prefix": "+34", "number": "612345678"}
        user.refresh_from_db()
        assert user.phone_prefix == "+34"
        assert user.phone == "612345678"

    def test_clear_phone(self, authed_client, user):
        user.phone_prefix = "+1"
        user.phone = "5551234"
        user.save(update_fields=["phone_prefix", "phone"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["phone"] is None
        user.refresh_from_db()
        assert user.phone_prefix is None
        assert user.phone is None

    def test_update_timezone(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"timezone": "Europe/Madrid"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["timezone"] == "Europe/Madrid"

    def test_update_job_title(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": "Engineer"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["job_title"] == "Engineer"

    def test_update_pronouns(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": "they/them"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["pronouns"] == "they/them"

    def test_update_bio(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": "Hello world"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["bio"] == "Hello world"

    def test_update_invalid_phone_prefix_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": {"prefix": "+9999", "number": "123456"}},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_bio_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": "x" * 501},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_job_title_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": "x" * 101},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_pronouns_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": "x" * 51},
            format="json",
        )
        assert resp.status_code == 400

    def test_clear_timezone(self, authed_client, user):
        user.timezone = "Europe/Madrid"
        user.save(update_fields=["timezone"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"timezone": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["timezone"] is None
        user.refresh_from_db()
        assert user.timezone is None

    def test_clear_bio(self, authed_client, user):
        user.bio = "Some bio"
        user.save(update_fields=["bio"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["bio"] is None
        user.refresh_from_db()
        assert user.bio is None

    def test_clear_job_title(self, authed_client, user):
        user.job_title = "Engineer"
        user.save(update_fields=["job_title"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["job_title"] is None

    def test_clear_pronouns(self, authed_client, user):
        user.pronouns = "she/her"
        user.save(update_fields=["pronouns"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["pronouns"] is None

    def test_unauthenticated_patch_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/account/", {"full_name": "Hacker"}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountViewDELETE:
    @patch("apps.users.views._billing_repos", return_value=(MagicMock(), MagicMock()))
    @patch("apps.users.views.delete_account", new_callable=AsyncMock, return_value=None)
    def test_delete_immediate_returns_204(self, mock_delete, _mock_repos, authed_client, user):
        resp = authed_client.delete("/api/v1/account/")
        assert resp.status_code == 204
        mock_delete.assert_called_once()
        call_kwargs = mock_delete.call_args.kwargs
        assert call_kwargs["user_id"] == user.id

    def test_unauthenticated_delete_rejected(self):
        client = APIClient()
        resp = client.delete("/api/v1/account/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountExportView:
    @patch("apps.users.views._billing_repos", return_value=(MagicMock(), MagicMock()))
    @patch("apps.users.views.export_user_data", new_callable=AsyncMock)
    def test_export_returns_data(self, mock_export, _mock_repos, authed_client, user):
        mock_export.return_value = {"user": {"email": user.email}}
        resp = authed_client.get("/api/v1/account/export/")
        assert resp.status_code == 200
        assert resp.data["user"]["email"] == user.email
        mock_export.assert_called_once()

    def test_unauthenticated_export_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/account/export/")
        assert resp.status_code in (401, 403)
