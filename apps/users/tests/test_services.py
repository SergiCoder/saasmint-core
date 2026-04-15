"""Tests for apps.users.services — resolve_oauth_user."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthEmailNotVerifiedError, OAuthUserInfo
from apps.users.services import resolve_oauth_user


def _info(
    email: str = "oauth@example.com",
    full_name: str = "OAuth User",
    provider_user_id: str = "12345",
    avatar_url: str | None = "https://example.com/avatar.png",
    email_verified: bool = True,
) -> OAuthUserInfo:
    return OAuthUserInfo(
        email=email,
        full_name=full_name,
        provider_user_id=provider_user_id,
        avatar_url=avatar_url,
        email_verified=email_verified,
    )


@pytest.mark.django_db
class TestResolveOAuthUserNewUser:
    @patch("apps.users.services.assign_free_plan")
    def test_creates_new_user(self, mock_plan):
        user = resolve_oauth_user("google", _info())

        assert user.email == "oauth@example.com"
        assert user.full_name == "OAuth User"
        assert user.avatar_url == "https://example.com/avatar.png"
        assert user.is_verified is True
        assert user.registration_method == "google"
        assert user.has_usable_password() is False
        mock_plan.assert_called_once_with(user)

    @patch("apps.users.services.assign_free_plan")
    def test_creates_social_account(self, _mock_plan):
        user = resolve_oauth_user("github", _info(provider_user_id="gh-new"))

        social = SocialAccount.objects.get(user=user, provider="github")
        assert social.provider_user_id == "gh-new"


@pytest.mark.django_db
class TestResolveOAuthUserExistingEmail:
    def test_auto_links_social_account(self):
        existing = User.objects.create_user(
            email="existing@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing User",
        )
        info = _info(email="existing@example.com", provider_user_id="g-link")

        user = resolve_oauth_user("google", info)

        assert user.pk == existing.pk
        # registration_method stays as original
        assert user.registration_method == "email"
        assert SocialAccount.objects.filter(user=user, provider="google").exists()

    def test_does_not_assign_free_plan_for_existing_user(self):
        User.objects.create_user(
            email="nofree@example.com",
            password="testpass123",  # noqa: S106
            full_name="No Free",
        )
        info = _info(email="nofree@example.com", provider_user_id="g-nofree")

        with patch("apps.users.services.assign_free_plan") as mock_plan:
            resolve_oauth_user("google", info)
        mock_plan.assert_not_called()


@pytest.mark.django_db
class TestResolveOAuthUserReturningSocial:
    def test_finds_user_by_social_account(self):
        user = User.objects.create_user(
            email="returning@example.com",
            full_name="Returning",
            registration_method="github",
        )
        SocialAccount.objects.create(user=user, provider="github", provider_user_id="gh-ret")

        info = _info(email="returning@example.com", provider_user_id="gh-ret")
        result = resolve_oauth_user("github", info)
        assert result.pk == user.pk

    def test_does_not_duplicate_social_account(self):
        user = User.objects.create_user(
            email="nodup@example.com",
            full_name="No Dup",
            registration_method="google",
        )
        SocialAccount.objects.create(user=user, provider="google", provider_user_id="g-nodup")

        info = _info(email="nodup@example.com", provider_user_id="g-nodup")
        resolve_oauth_user("google", info)
        assert SocialAccount.objects.filter(user=user, provider="google").count() == 1


@pytest.mark.django_db
class TestResolveOAuthUserUnverifiedEmail:
    def test_unverified_email_refuses_to_create_new_user(self):
        info = _info(email="unverified@example.com", email_verified=False)
        with pytest.raises(OAuthEmailNotVerifiedError):
            resolve_oauth_user("microsoft", info)
        assert not User.objects.filter(email="unverified@example.com").exists()

    def test_unverified_email_refuses_to_link_existing_user(self):
        User.objects.create_user(
            email="victim@example.com",
            password="testpass123",  # noqa: S106
            full_name="Victim",
        )
        info = _info(
            email="victim@example.com",
            provider_user_id="ms-attacker",
            email_verified=False,
        )
        with pytest.raises(OAuthEmailNotVerifiedError):
            resolve_oauth_user("microsoft", info)
        assert not SocialAccount.objects.filter(
            provider="microsoft", provider_user_id="ms-attacker"
        ).exists()

    def test_returning_social_account_bypasses_verified_check(self):
        """Already-linked SocialAccount can log in even if current response
        omits email verification — the link was established earlier."""
        user = User.objects.create_user(
            email="linked@example.com",
            full_name="Linked",
            registration_method="microsoft",
        )
        SocialAccount.objects.create(user=user, provider="microsoft", provider_user_id="ms-linked")
        info = _info(
            email="linked@example.com",
            provider_user_id="ms-linked",
            email_verified=False,
        )
        result = resolve_oauth_user("microsoft", info)
        assert result.pk == user.pk
