"""Tests for apps.users.tasks — periodic cleanup Celery tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import AccountType, RefreshToken, User
from apps.users.tasks import (
    cleanup_expired_refresh_tokens,
    cleanup_orphaned_org_accounts,
)


def _run(task) -> None:
    """Apply a Celery task eagerly (bypasses the worker)."""
    task.apply().get()


@pytest.mark.django_db
class TestCleanupOrphanedOrgAccounts:
    def _backdate(self, user: User, hours: int) -> None:
        User.objects.filter(pk=user.pk).update(
            created_at=datetime.now(UTC) - timedelta(hours=hours)
        )

    def test_deletes_org_member_with_no_membership_older_than_24h(self):
        orphan = User.objects.create_user(
            email="orphan@example.com",
            full_name="Orphan Member",
            account_type=AccountType.ORG_MEMBER,
        )
        self._backdate(orphan, hours=48)

        _run(cleanup_orphaned_org_accounts)

        assert not User.objects.filter(pk=orphan.pk).exists()

    def test_keeps_org_member_created_within_cutoff(self):
        recent = User.objects.create_user(
            email="recent@example.com",
            full_name="Recent Member",
            account_type=AccountType.ORG_MEMBER,
        )

        _run(cleanup_orphaned_org_accounts)

        assert User.objects.filter(pk=recent.pk).exists()

    def test_keeps_org_member_with_membership(self):
        user = User.objects.create_user(
            email="member@example.com",
            full_name="Active Member",
            account_type=AccountType.ORG_MEMBER,
        )
        self._backdate(user, hours=48)
        org = Org.objects.create(name="Acme", slug="acme")
        OrgMember.objects.create(org=org, user=user, role=OrgRole.MEMBER)

        _run(cleanup_orphaned_org_accounts)

        assert User.objects.filter(pk=user.pk).exists()

    def test_keeps_personal_account_even_without_membership(self):
        personal = User.objects.create_user(
            email="personal@example.com",
            full_name="Personal User",
            account_type=AccountType.PERSONAL,
        )
        self._backdate(personal, hours=48)

        _run(cleanup_orphaned_org_accounts)

        assert User.objects.filter(pk=personal.pk).exists()

    def test_noop_when_nothing_to_delete(self):
        # Task should run cleanly with an empty user table.
        _run(cleanup_orphaned_org_accounts)

    def test_only_targets_orphans_past_cutoff(self):
        keep = User.objects.create_user(
            email="keep@example.com",
            full_name="Keep",
            account_type=AccountType.ORG_MEMBER,
        )
        self._backdate(keep, hours=23)
        drop = User.objects.create_user(
            email="drop@example.com",
            full_name="Drop",
            account_type=AccountType.ORG_MEMBER,
        )
        self._backdate(drop, hours=25)

        _run(cleanup_orphaned_org_accounts)

        assert User.objects.filter(pk=keep.pk).exists()
        assert not User.objects.filter(pk=drop.pk).exists()


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
