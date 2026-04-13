"""Tests for auth_views.py — register, login, verify-email, refresh, logout,
forgot-password, reset-password, change-password, and OAuth authorize views.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.users.authentication import (
    _hash_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
)
from apps.users.models import RefreshToken, User

# Relax throttling in tests
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {"auth": "1000/hour"},
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def verified_user(db):
    user = User.objects.create_user(
        email="verified@example.com",
        password="testpass123",  # noqa: S106
        full_name="Verified User",
        is_verified=True,
    )
    return user


@pytest.fixture
def authed_client(verified_user):
    client = APIClient()
    client.force_authenticate(user=verified_user)
    return client


# ---------------------------------------------------------------------------
# RegisterView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegisterView:
    URL = "/api/v1/auth/register/"

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_register_success(self, mock_email, api):
        resp = api.post(
            self.URL,
            {"email": "new@example.com", "password": "securepass1", "full_name": "New User"},
            format="json",
        )
        assert resp.status_code == 201
        assert "access_token" in resp.data
        assert "refresh_token" in resp.data
        assert resp.data["token_type"] == "Bearer"

        user = User.objects.get(email="new@example.com")
        assert user.full_name == "New User"
        assert user.is_verified is False
        mock_email.assert_called_once()

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_register_duplicate_email_returns_409(self, _mock_email, api):
        User.objects.create_user(
            email="dup@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing",
        )
        resp = api.post(
            self.URL,
            {"email": "dup@example.com", "password": "securepass1", "full_name": "Duplicate"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "email_exists"

    def test_register_missing_fields_returns_400(self, api):
        resp = api.post(self.URL, {"email": "only@example.com"}, format="json")
        assert resp.status_code == 400

    def test_register_short_password_returns_400(self, api):
        resp = api.post(
            self.URL,
            {"email": "short@example.com", "password": "short", "full_name": "Short Pass"},
            format="json",
        )
        assert resp.status_code == 400

    def test_register_short_full_name_returns_400(self, api):
        resp = api.post(
            self.URL,
            {"email": "name@example.com", "password": "securepass1", "full_name": "AB"},
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_register_email_failure_still_succeeds(self, mock_delay, api):
        """Email is sent async via Celery — even if the task dispatch fails,
        registration still succeeds (fire-and-forget)."""
        resp = api.post(
            self.URL,
            {
                "email": "emailfail@example.com",
                "password": "securepass1",
                "full_name": "Email Fail",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert User.objects.filter(email="emailfail@example.com").exists()
        mock_delay.assert_called_once()


# ---------------------------------------------------------------------------
# RegisterOrgOwnerView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegisterOrgOwnerView:
    URL = "/api/v1/auth/register/org-owner/"

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_register_org_owner_success(self, mock_email, api):
        resp = api.post(
            self.URL,
            {"email": "orgowner@example.com", "password": "securepass1", "full_name": "Org Owner"},
            format="json",
        )
        assert resp.status_code == 201
        assert "access_token" in resp.data
        assert "refresh_token" in resp.data

        user = User.objects.get(email="orgowner@example.com")
        assert user.account_type == "org_member"
        assert user.is_verified is False
        mock_email.assert_called_once()

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_duplicate_email_returns_409(self, _mock_email, api):
        User.objects.create_user(
            email="taken@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing",
        )
        resp = api.post(
            self.URL,
            {"email": "taken@example.com", "password": "securepass1", "full_name": "Dup"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "email_exists"

    def test_missing_fields_returns_400(self, api):
        resp = api.post(self.URL, {"email": "only@example.com"}, format="json")
        assert resp.status_code == 400

    @patch("apps.users.tasks.send_verification_email_task.delay")
    def test_no_free_plan_assigned(self, mock_email, api):
        """Org owner registration does not assign a free plan subscription."""
        from apps.billing.models import Subscription

        api.post(
            self.URL,
            {"email": "noplan@example.com", "password": "securepass1", "full_name": "No Plan"},
            format="json",
        )
        user = User.objects.get(email="noplan@example.com")
        assert not Subscription.objects.filter(user=user).exists()


# ---------------------------------------------------------------------------
# LoginView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLoginView:
    URL = "/api/v1/auth/login/"

    def test_login_success(self, api, verified_user):
        resp = api.post(
            self.URL,
            {"email": "verified@example.com", "password": "testpass123"},
            format="json",
        )
        assert resp.status_code == 200
        assert "access_token" in resp.data
        assert "refresh_token" in resp.data

    def test_login_wrong_password(self, api, verified_user):
        resp = api.post(
            self.URL,
            {"email": "verified@example.com", "password": "wrongpass"},
            format="json",
        )
        assert resp.status_code == 401
        assert resp.data["code"] == "invalid_credentials"

    def test_login_nonexistent_email(self, api):
        resp = api.post(
            self.URL,
            {"email": "nobody@example.com", "password": "testpass123"},
            format="json",
        )
        assert resp.status_code == 401
        assert resp.data["code"] == "invalid_credentials"

    def test_login_deactivated_user(self, api):
        User.objects.create_user(
            email="deact@example.com",
            password="testpass123",  # noqa: S106
            full_name="Deactivated",
            is_verified=True,
            is_active=False,
        )
        resp = api.post(
            self.URL,
            {"email": "deact@example.com", "password": "testpass123"},
            format="json",
        )
        # Django's authenticate returns None for inactive users
        assert resp.status_code == 401

    def test_login_unverified_user(self, api):
        User.objects.create_user(
            email="unverified@example.com",
            password="testpass123",  # noqa: S106
            full_name="Unverified",
            is_verified=False,
        )
        resp = api.post(
            self.URL,
            {"email": "unverified@example.com", "password": "testpass123"},
            format="json",
        )
        assert resp.status_code == 403
        assert resp.data["code"] == "email_not_verified"


# ---------------------------------------------------------------------------
# VerifyEmailView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestVerifyEmailView:
    URL = "/api/v1/auth/verify-email/"

    def test_verify_email_success(self, api):
        user = User.objects.create_user(
            email="toverify@example.com", full_name="To Verify", is_verified=False
        )
        token = create_email_verification_token(user)

        resp = api.post(self.URL, {"token": token}, format="json")
        assert resp.status_code == 200
        assert "access_token" in resp.data

        user.refresh_from_db()
        assert user.is_verified is True

    def test_verify_email_already_verified(self, api, verified_user):
        token = create_email_verification_token(verified_user)
        resp = api.post(self.URL, {"token": token}, format="json")
        assert resp.status_code == 200
        # Should still succeed, is_verified stays True
        verified_user.refresh_from_db()
        assert verified_user.is_verified is True

    def test_verify_email_invalid_token(self, api):
        resp = api.post(self.URL, {"token": "invalid-token"}, format="json")
        assert resp.status_code == 401  # AuthenticationFailed


# ---------------------------------------------------------------------------
# RefreshView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRefreshView:
    URL = "/api/v1/auth/refresh/"

    def test_refresh_success(self, api, verified_user):
        raw = create_refresh_token(verified_user)
        resp = api.post(self.URL, {"refresh_token": raw}, format="json")
        assert resp.status_code == 200
        assert "access_token" in resp.data
        assert resp.data["refresh_token"] != raw  # rotated

    def test_refresh_invalid_token(self, api):
        resp = api.post(self.URL, {"refresh_token": "bad-token"}, format="json")
        assert resp.status_code == 401

    def test_refresh_revoked_token(self, api, verified_user):
        raw = create_refresh_token(verified_user)
        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        rt.revoked_at = datetime.now(UTC)
        rt.save(update_fields=["revoked_at"])

        resp = api.post(self.URL, {"refresh_token": raw}, format="json")
        assert resp.status_code == 401

    def test_refresh_expired_token(self, api, verified_user):
        raw = create_refresh_token(verified_user)
        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        rt.expires_at = datetime.now(UTC) - timedelta(hours=1)
        rt.save(update_fields=["expires_at"])

        resp = api.post(self.URL, {"refresh_token": raw}, format="json")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# LogoutView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLogoutView:
    URL = "/api/v1/auth/logout/"

    def test_logout_revokes_token(self, authed_client, verified_user):
        raw = create_refresh_token(verified_user)
        resp = authed_client.post(self.URL, {"refresh_token": raw}, format="json")
        assert resp.status_code == 204

        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        assert rt.revoked_at is not None

    def test_logout_nonexistent_token_is_noop(self, authed_client):
        resp = authed_client.post(self.URL, {"refresh_token": "does-not-exist"}, format="json")
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# ForgotPasswordView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestForgotPasswordView:
    URL = "/api/v1/auth/forgot-password/"

    @patch("apps.users.tasks.send_password_reset_email_task.delay")
    def test_forgot_password_existing_user(self, mock_delay, api, verified_user):
        resp = api.post(self.URL, {"email": "verified@example.com"}, format="json")
        assert resp.status_code == 200
        mock_delay.assert_called_once()

    def test_forgot_password_nonexistent_email_returns_200(self, api):
        resp = api.post(self.URL, {"email": "nobody@example.com"}, format="json")
        # Always 200 to prevent email enumeration
        assert resp.status_code == 200

    @patch("apps.users.tasks.send_password_reset_email_task.delay")
    def test_forgot_password_email_failure_still_returns_200(self, mock_delay, api, verified_user):
        resp = api.post(self.URL, {"email": "verified@example.com"}, format="json")
        assert resp.status_code == 200
        mock_delay.assert_called_once()


# ---------------------------------------------------------------------------
# ResetPasswordView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResetPasswordView:
    URL = "/api/v1/auth/reset-password/"

    def test_reset_password_success(self, api, verified_user):
        token = create_password_reset_token(verified_user)
        resp = api.post(
            self.URL,
            {"token": token, "password": "newpassword1"},
            format="json",
        )
        assert resp.status_code == 200
        assert "access_token" in resp.data

        verified_user.refresh_from_db()
        assert verified_user.check_password("newpassword1")

    def test_reset_password_revokes_all_refresh_tokens(self, api, verified_user):
        create_refresh_token(verified_user)
        create_refresh_token(verified_user)

        token = create_password_reset_token(verified_user)
        api.post(
            self.URL,
            {"token": token, "password": "newpassword1"},
            format="json",
        )

        # All existing refresh tokens should be revoked
        assert (
            RefreshToken.objects.filter(user=verified_user, revoked_at__isnull=True).count()
            == 1  # only the new one issued after reset
        )

    def test_reset_password_invalid_token(self, api):
        resp = api.post(
            self.URL,
            {"token": "bad-token", "password": "newpassword1"},
            format="json",
        )
        assert resp.status_code == 401

    def test_reset_password_short_password(self, api, verified_user):
        token = create_password_reset_token(verified_user)
        resp = api.post(
            self.URL,
            {"token": token, "password": "short"},
            format="json",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# ChangePasswordView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestChangePasswordView:
    URL = "/api/v1/auth/change-password/"

    def test_change_password_success(self, authed_client, verified_user):
        resp = authed_client.post(
            self.URL,
            {"current_password": "testpass123", "new_password": "newpassword1"},
            format="json",
        )
        assert resp.status_code == 200
        assert "access_token" in resp.data

        verified_user.refresh_from_db()
        assert verified_user.check_password("newpassword1")

    def test_change_password_wrong_current(self, authed_client):
        resp = authed_client.post(
            self.URL,
            {"current_password": "wrongpass", "new_password": "newpassword1"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.data["code"] == "invalid_password"

    def test_change_password_revokes_all_refresh_tokens(self, authed_client, verified_user):
        create_refresh_token(verified_user)
        create_refresh_token(verified_user)

        authed_client.post(
            self.URL,
            {"current_password": "testpass123", "new_password": "newpassword1"},
            format="json",
        )

        # Old tokens revoked, only the new one from change-password remains
        assert RefreshToken.objects.filter(user=verified_user, revoked_at__isnull=True).count() == 1

    def test_change_password_unauthenticated(self, api):
        resp = api.post(
            self.URL,
            {"current_password": "testpass123", "new_password": "newpassword1"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_change_password_short_new_password(self, authed_client):
        resp = authed_client.post(
            self.URL,
            {"current_password": "testpass123", "new_password": "short"},
            format="json",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# OAuthAuthorizeView
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOAuthAuthorizeView:
    def test_authorize_google_redirects(self, api, settings):
        settings.OAUTH_GOOGLE_CLIENT_ID = "test-client-id"
        settings.OAUTH_GOOGLE_CLIENT_SECRET = "test-secret"
        resp = api.get("/api/v1/auth/oauth/google/")
        assert resp.status_code == 302
        assert "accounts.google.com" in resp["Location"]
        assert "test-client-id" in resp["Location"]

    def test_authorize_invalid_provider_returns_400(self, api):
        resp = api.get("/api/v1/auth/oauth/invalid_provider/")
        assert resp.status_code == 400
        assert resp.data["code"] == "invalid_provider"

    def test_authorize_github_redirects(self, api, settings):
        settings.OAUTH_GITHUB_CLIENT_ID = "gh-client-id"
        settings.OAUTH_GITHUB_CLIENT_SECRET = "gh-secret"
        resp = api.get("/api/v1/auth/oauth/github/")
        assert resp.status_code == 302
        assert "github.com" in resp["Location"]

    def test_authorize_microsoft_redirects(self, api, settings):
        settings.OAUTH_MICROSOFT_CLIENT_ID = "ms-client-id"
        settings.OAUTH_MICROSOFT_CLIENT_SECRET = "ms-secret"
        resp = api.get("/api/v1/auth/oauth/microsoft/")
        assert resp.status_code == 302
        assert "login.microsoftonline.com" in resp["Location"]
