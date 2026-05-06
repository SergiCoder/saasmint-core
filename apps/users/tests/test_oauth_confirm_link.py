"""Tests for OAuthConfirmLinkView — email-confirm path for cross-provider OAuth linking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient

from apps.users.authentication import create_social_link_token
from apps.users.models import SocialAccount, SocialLinkRequest, User

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


URL = "/api/v1/auth/oauth/confirm-link/"


def _mint(
    user: User,
    *,
    provider: str = "microsoft",
    provider_user_id: str = "ms-1",
    full_name: str = "MS User",
    avatar_url: str | None = None,
) -> str:
    return create_social_link_token(
        user,
        provider=provider,
        provider_user_id=provider_user_id,
        full_name=full_name,
        avatar_url=avatar_url,
    )


@pytest.mark.django_db
class TestConfirmLinkHappyPath:
    def test_creates_social_account_marks_verified_and_issues_tokens(self, api):
        user = User.objects.create_user(
            email="bob@example.com",
            full_name="Bob",
            registration_method="google",
            is_verified=False,
        )
        token = _mint(user)

        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "Bearer"
        # Mirror /oauth/exchange/ envelope so the frontend reuses
        # setAuthCookies(access, refresh, expires_in) from its OAuth path.
        assert body["expires_in"] == 15 * 60

        assert SocialAccount.objects.filter(
            user=user, provider="microsoft", provider_user_id="ms-1"
        ).exists()
        user.refresh_from_db()
        assert user.is_verified is True

        # Token marked used
        link_req = SocialLinkRequest.objects.get(user=user)
        assert link_req.used_at is not None

    def test_does_not_re_save_already_verified_user(self, api):
        user = User.objects.create_user(
            email="alice@example.com",
            full_name="Alice",
            registration_method="google",
            is_verified=True,
        )
        original_updated = user.updated_at
        token = _mint(user)

        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.is_verified is True
        assert user.updated_at == original_updated


@pytest.mark.django_db
class TestConfirmLinkRejection:
    def test_replay_rejected(self, api):
        user = User.objects.create_user(
            email="bob@example.com", full_name="Bob", registration_method="google"
        )
        token = _mint(user)
        first = api.post(URL, {"token": token}, format="json")
        assert first.status_code == 200
        second = api.post(URL, {"token": token}, format="json")
        assert second.status_code in (401, 403)
        assert second.data["code"] == "token_used"

    def test_expired_rejected(self, api):
        user = User.objects.create_user(
            email="bob@example.com", full_name="Bob", registration_method="google"
        )
        token = _mint(user)
        SocialLinkRequest.objects.filter(user=user).update(
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code in (401, 403)
        assert resp.data["code"] == "token_expired"

    def test_invalid_token_rejected(self, api):
        resp = api.post(URL, {"token": "bogus-not-a-real-token"}, format="json")
        assert resp.status_code in (401, 403)
        assert resp.data["code"] == "invalid_token"

    def test_inactive_user_rejected(self, api):
        user = User.objects.create_user(
            email="bob@example.com",
            full_name="Bob",
            registration_method="google",
        )
        token = _mint(user)
        user.is_active = False
        user.save(update_fields=["is_active"])

        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code in (401, 403)
        assert resp.data["code"] == "user_not_found"

    def test_provider_account_already_linked_to_other_user(self, api):
        """If the (provider, provider_user_id) row already belongs to a
        different user (extremely unusual — would require a race or stale
        token), the endpoint refuses with 409."""
        owner = User.objects.create_user(
            email="owner@example.com", full_name="Owner", registration_method="google"
        )
        SocialAccount.objects.create(
            user=owner, provider="microsoft", provider_user_id="ms-shared"
        )

        intruder = User.objects.create_user(
            email="intruder@example.com",
            full_name="Intruder",
            registration_method="google",
        )
        token = _mint(intruder, provider_user_id="ms-shared")

        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code == 409
        assert resp.data["code"] == "social_account_collision"


@pytest.mark.django_db
class TestConfirmLinkIdempotency:
    def test_user_already_has_provider_row_succeeds(self, api):
        """If the user already has a SocialAccount for this (provider,
        provider_user_id) — e.g. duplicate-token replay window — the
        endpoint succeeds rather than failing the user."""
        user = User.objects.create_user(
            email="bob@example.com", full_name="Bob", registration_method="google"
        )
        SocialAccount.objects.create(
            user=user, provider="microsoft", provider_user_id="ms-1"
        )
        token = _mint(user)

        resp = api.post(URL, {"token": token}, format="json")
        assert resp.status_code == 200
        assert (
            SocialAccount.objects.filter(
                user=user, provider="microsoft", provider_user_id="ms-1"
            ).count()
            == 1
        )
