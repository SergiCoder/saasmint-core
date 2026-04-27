"""Tests for apps.users.services — resolve_oauth_user."""

from __future__ import annotations

import pytest

from apps.users.models import SocialAccount, User
from apps.users.oauth import (
    OAuthEmailNotVerifiedError,
    OAuthEmailUnverifiedCollisionError,
    OAuthUserInfo,
)
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
    def test_creates_new_user(self):
        user = resolve_oauth_user("google", _info())

        assert user.email == "oauth@example.com"
        assert user.full_name == "OAuth User"
        assert user.avatar_url == "https://example.com/avatar.png"
        assert user.is_verified is True
        assert user.registration_method == "google"
        assert user.has_usable_password() is False

    def test_creates_social_account(self):
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
        """Unverified email + existing local account → collision error
        (specifically, NOT the generic email-not-verified error). Frontend
        uses the collision code to guide the user to log in with their
        password and link the provider explicitly."""
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
        with pytest.raises(OAuthEmailUnverifiedCollisionError):
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


@pytest.mark.django_db
class TestResolveOAuthUserTrustList:
    """The auto-link trust list is defense-in-depth. Today every supported
    provider (google, github, microsoft) is on the list, so the untrusted-
    provider branch is exercised via a hypothetical/future provider name.
    See ``apps.users.services.TRUSTED_FOR_AUTO_LINK``."""

    def test_github_auto_links_existing_user(self):
        """GitHub's ``verified`` flag from /user/emails primary is
        comparable strength to Google's email verification — the user
        clicked a link the provider sent. Both are trusted."""
        existing = User.objects.create_user(
            email="gh-existing@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing",
        )
        info = _info(
            email="gh-existing@example.com",
            provider_user_id="gh-link-1",
            email_verified=True,
        )
        user = resolve_oauth_user("github", info)
        assert user.pk == existing.pk
        assert SocialAccount.objects.filter(
            user=user, provider="github", provider_user_id="gh-link-1"
        ).exists()

    def test_untrusted_provider_collides_on_existing_user(self):
        """A provider not on the trust list cannot auto-link onto an
        existing local account, even when ``email_verified`` is True."""
        User.objects.create_user(
            email="trusted@example.com",
            password="testpass123",  # noqa: S106
            full_name="Trusted",
        )
        info = _info(
            email="trusted@example.com",
            provider_user_id="future-1",
            email_verified=True,
        )
        with pytest.raises(OAuthEmailUnverifiedCollisionError):
            resolve_oauth_user("future_provider", info)
        assert not SocialAccount.objects.filter(provider="future_provider").exists()

    def test_untrusted_provider_creates_new_user_when_no_collision(self):
        """An untrusted provider can still create a brand-new account on
        first login — the trust list only gates the existing-user
        auto-link path."""
        info = _info(
            email="brand-new@example.com",
            provider_user_id="future-new",
            email_verified=True,
        )
        user = resolve_oauth_user("future_provider", info)
        assert user.email == "brand-new@example.com"
        assert SocialAccount.objects.filter(
            user=user, provider="future_provider", provider_user_id="future-new"
        ).exists()
