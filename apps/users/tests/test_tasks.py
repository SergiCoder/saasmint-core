"""Tests for apps.users.tasks — periodic cleanup Celery tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.users.models import RefreshToken, SocialLinkRequest, User
from apps.users.tasks import (
    cleanup_expired_refresh_tokens,
    cleanup_expired_social_link_requests,
)


def _run(task) -> None:
    """Apply a Celery task eagerly (bypasses the worker)."""
    task.apply().get()


@pytest.mark.django_db
class TestCleanupExpiredRefreshTokens:
    def _user(self, email: str = "rt@example.com") -> User:
        return User.objects.create_user(email=email, full_name="RT User")

    def test_deletes_expired_token(self):
        user = self._user()
        expired = RefreshToken.objects.create(
            user=user,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )

        _run(cleanup_expired_refresh_tokens)

        assert not RefreshToken.objects.filter(pk=expired.pk).exists()

    def test_keeps_live_token(self):
        user = self._user()
        live = RefreshToken.objects.create(
            user=user,
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )

        _run(cleanup_expired_refresh_tokens)

        assert RefreshToken.objects.filter(pk=live.pk).exists()

    def test_keeps_revoked_but_unexpired_token(self):
        # Revoked tokens with future expiry should survive this task; a
        # separate policy decides if/when to purge revoked rows.
        user = self._user()
        revoked = RefreshToken.objects.create(
            user=user,
            token_hash="c" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            revoked_at=datetime.now(UTC),
        )

        _run(cleanup_expired_refresh_tokens)

        assert RefreshToken.objects.filter(pk=revoked.pk).exists()

    def test_deletes_expired_revoked_token(self):
        user = self._user()
        expired_revoked = RefreshToken.objects.create(
            user=user,
            token_hash="d" * 64,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            revoked_at=datetime.now(UTC) - timedelta(hours=1),
        )

        _run(cleanup_expired_refresh_tokens)

        assert not RefreshToken.objects.filter(pk=expired_revoked.pk).exists()

    def test_noop_when_no_tokens(self):
        _run(cleanup_expired_refresh_tokens)

    def test_deletes_only_expired_in_mixed_set(self):
        user = self._user()
        live = RefreshToken.objects.create(
            user=user,
            token_hash="e" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        stale = RefreshToken.objects.create(
            user=user,
            token_hash="f" * 64,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        _run(cleanup_expired_refresh_tokens)

        assert RefreshToken.objects.filter(pk=live.pk).exists()
        assert not RefreshToken.objects.filter(pk=stale.pk).exists()


@pytest.mark.django_db
class TestCleanupExpiredSocialLinkRequests:
    def _user(self, email: str = "slr@example.com") -> User:
        return User.objects.create_user(email=email, full_name="SLR User")

    def test_deletes_expired_row(self):
        user = self._user()
        expired = SocialLinkRequest.objects.create(
            user=user,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            provider="microsoft",
            provider_user_id="ms-1",
            full_name="",
        )
        _run(cleanup_expired_social_link_requests)
        assert not SocialLinkRequest.objects.filter(pk=expired.pk).exists()

    def test_keeps_live_row(self):
        user = self._user()
        live = SocialLinkRequest.objects.create(
            user=user,
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            provider="microsoft",
            provider_user_id="ms-2",
            full_name="",
        )
        _run(cleanup_expired_social_link_requests)
        assert SocialLinkRequest.objects.filter(pk=live.pk).exists()

    def test_noop_when_empty(self):
        _run(cleanup_expired_social_link_requests)
