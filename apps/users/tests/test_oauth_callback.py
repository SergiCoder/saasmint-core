"""Tests for OAuthCallbackView — user lookup, auto-linking, and SocialAccount creation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthUserInfo

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
def _oauth_state(client):
    """Set a valid OAuth state in the session so the callback can verify it."""
    session = client.session
    session["oauth_state"] = "test-state"
    session.save()


def _mock_exchange(
    email: str = "oauth@example.com",
    provider_user_id: str = "12345",
    email_verified: bool = True,
):
    return OAuthUserInfo(
        email=email,
        full_name="OAuth User",
        provider_user_id=provider_user_id,
        avatar_url="https://example.com/avatar.png",
        email_verified=email_verified,
    )


@pytest.mark.django_db
class TestOAuthCallbackNewUser:
    def test_creates_user_and_social_account(self, client, _oauth_state):
        with patch("apps.users.oauth.exchange_code", return_value=_mock_exchange()):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "access_token" in resp["Location"]

        user = User.objects.get(email="oauth@example.com")
        assert user.registration_method == "google"
        assert user.is_verified is True
        assert user.has_usable_password() is False

        social = SocialAccount.objects.get(user=user, provider="google")
        assert social.provider_user_id == "12345"

    def test_assigns_free_plan(self, client, _oauth_state):
        with (
            patch("apps.users.oauth.exchange_code", return_value=_mock_exchange()),
            patch("apps.users.services.assign_free_plan") as mock_plan,
        ):
            client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        mock_plan.assert_called_once()


@pytest.mark.django_db
class TestOAuthCallbackExistingEmailUser:
    def test_auto_links_social_account(self, client, _oauth_state):
        user = User.objects.create_user(
            email="existing@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing User",
        )
        info = _mock_exchange(email="existing@example.com", provider_user_id="g-99")
        with patch("apps.users.oauth.exchange_code", return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "access_token" in resp["Location"]

        # User keeps original registration_method
        user.refresh_from_db()
        assert user.registration_method == "email"

        # SocialAccount was auto-linked
        assert SocialAccount.objects.filter(user=user, provider="google").exists()


@pytest.mark.django_db
class TestOAuthCallbackReturningSocialUser:
    def test_finds_user_by_social_account(self, client, _oauth_state):
        user = User.objects.create_user(
            email="returning@example.com",
            full_name="Returning User",
            registration_method="github",
        )
        SocialAccount.objects.create(user=user, provider="github", provider_user_id="gh-42")

        info = _mock_exchange(email="returning@example.com", provider_user_id="gh-42")
        with patch("apps.users.oauth.exchange_code", return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/github/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "access_token" in resp["Location"]

        # No duplicate SocialAccount created
        assert SocialAccount.objects.filter(user=user, provider="github").count() == 1


@pytest.mark.django_db
class TestOAuthCallbackTokensInFragment:
    def test_tokens_are_placed_in_url_fragment(self, client, _oauth_state):
        with patch("apps.users.oauth.exchange_code", return_value=_mock_exchange()):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        location = resp["Location"]
        # Tokens must be in the fragment (after #), not the query string.
        assert "#access_token=" in location
        assert "?access_token=" not in location


@pytest.mark.django_db
class TestOAuthCallbackUnverifiedEmail:
    def test_unverified_email_blocks_new_user(self, client, _oauth_state):
        info = _mock_exchange(email="unverified@example.com", email_verified=False)
        with patch("apps.users.oauth.exchange_code", return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "email_not_verified" in resp["Location"]
        assert not User.objects.filter(email="unverified@example.com").exists()

    def test_unverified_email_blocks_linking_to_existing_account(self, client, _oauth_state):
        User.objects.create_user(
            email="victim@example.com",
            password="testpass123",  # noqa: S106
            full_name="Victim",
        )
        info = _mock_exchange(
            email="victim@example.com",
            provider_user_id="ms-attacker",
            email_verified=False,
        )
        with patch("apps.users.oauth.exchange_code", return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "email_not_verified" in resp["Location"]
        assert not SocialAccount.objects.filter(
            provider="microsoft", provider_user_id="ms-attacker"
        ).exists()


@pytest.mark.django_db
class TestOAuthCallbackDeactivatedUser:
    def test_deactivated_user_blocked(self, client, _oauth_state):
        user = User.objects.create_user(
            email="deact@example.com",
            full_name="Deact User",
            is_active=False,
            registration_method="google",
        )
        SocialAccount.objects.create(user=user, provider="google", provider_user_id="g-deact")

        info = _mock_exchange(email="deact@example.com", provider_user_id="g-deact")
        with patch("apps.users.oauth.exchange_code", return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "account_deactivated" in resp["Location"]
