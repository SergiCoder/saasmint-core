"""Tests for apps.users.tasks — periodic cleanup Celery tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from apps.users.models import (
    EmailVerificationToken,
    PasswordResetToken,
    RefreshToken,
    SocialLinkRequest,
    User,
)
from apps.users.tasks import (
    cleanup_expired_email_verification_tokens,
    cleanup_expired_password_reset_tokens,
    cleanup_expired_refresh_tokens,
    cleanup_expired_social_link_requests,
    send_social_link_email_task,
)


def _run(task) -> None:
    """Apply a Celery task eagerly (bypasses the worker)."""
    task.apply().get()


class TestSendSocialLinkEmailTask:
    """Unit tests for send_social_link_email_task — verifies the task body
    calls the underlying email helper with the correct arguments."""

    def test_delegates_to_send_social_link_email(self):
        with patch("apps.users.email.send_social_link_email") as mock_send:
            send_social_link_email_task.apply(
                args=["user@example.com", "tok_link", "microsoft"]
            ).get()

        mock_send.assert_called_once_with("user@example.com", "tok_link", "microsoft")

    def test_passes_provider_verbatim(self):
        with patch("apps.users.email.send_social_link_email") as mock_send:
            send_social_link_email_task.apply(args=["other@example.com", "tok_gh", "github"]).get()

        _, _, provider = mock_send.call_args.args
        assert provider == "github"


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


@pytest.mark.django_db
class TestCleanupExpiredEmailVerificationTokens:
    def _user(self, email: str = "evt@example.com") -> User:
        return User.objects.create_user(email=email, full_name="EVT User")

    def test_deletes_expired_token(self):
        user = self._user()
        expired = EmailVerificationToken.objects.create(
            user=user,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        _run(cleanup_expired_email_verification_tokens)
        assert not EmailVerificationToken.objects.filter(pk=expired.pk).exists()

    def test_keeps_live_token(self):
        user = self._user()
        live = EmailVerificationToken.objects.create(
            user=user,
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        _run(cleanup_expired_email_verification_tokens)
        assert EmailVerificationToken.objects.filter(pk=live.pk).exists()

    def test_email_verification_tokens_accumulate_without_cleanup(self):
        """Regression pin: without the cleanup task, used+expired rows are
        kept indefinitely. Insert two expired rows, run the task, and
        confirm both are pruned — the prior code shape (no task at all)
        would leave both behind."""
        user = self._user()
        for i in range(2):
            EmailVerificationToken.objects.create(
                user=user,
                token_hash=str(i) * 64,
                expires_at=datetime.now(UTC) - timedelta(days=i + 1),
                used_at=datetime.now(UTC) - timedelta(days=i),
            )
        assert EmailVerificationToken.objects.filter(user=user).count() == 2

        _run(cleanup_expired_email_verification_tokens)

        assert EmailVerificationToken.objects.filter(user=user).count() == 0

    def test_noop_when_empty(self):
        _run(cleanup_expired_email_verification_tokens)


@pytest.mark.django_db
class TestCleanupExpiredPasswordResetTokens:
    def _user(self, email: str = "prt@example.com") -> User:
        return User.objects.create_user(email=email, full_name="PRT User")

    def test_deletes_expired_token(self):
        user = self._user()
        expired = PasswordResetToken.objects.create(
            user=user,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        _run(cleanup_expired_password_reset_tokens)
        assert not PasswordResetToken.objects.filter(pk=expired.pk).exists()

    def test_keeps_live_token(self):
        user = self._user()
        live = PasswordResetToken.objects.create(
            user=user,
            token_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        _run(cleanup_expired_password_reset_tokens)
        assert PasswordResetToken.objects.filter(pk=live.pk).exists()

    def test_noop_when_empty(self):
        _run(cleanup_expired_password_reset_tokens)
