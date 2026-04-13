"""Tests for Celery tasks in the users app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import AccountType, User
from apps.users.tasks import cleanup_orphaned_org_accounts


@pytest.mark.django_db
class TestCleanupOrphanedOrgAccounts:
    def test_deletes_old_org_member_without_membership(self):
        user = User.objects.create_user(
            email="orphan@example.com",
            full_name="Orphan",
            account_type=AccountType.ORG_MEMBER,
        )
        # Backdate created_at so it's older than 24 hours
        User.objects.filter(id=user.id).update(created_at=datetime.now(UTC) - timedelta(hours=25))
        cleanup_orphaned_org_accounts()
        assert not User.objects.filter(id=user.id).exists()

    def test_keeps_recent_org_member_without_membership(self):
        user = User.objects.create_user(
            email="recent@example.com",
            full_name="Recent",
            account_type=AccountType.ORG_MEMBER,
        )
        cleanup_orphaned_org_accounts()
        assert User.objects.filter(id=user.id).exists()

    def test_keeps_org_member_with_membership(self):
        user = User.objects.create_user(
            email="member@example.com",
            full_name="Member",
            account_type=AccountType.ORG_MEMBER,
        )
        User.objects.filter(id=user.id).update(created_at=datetime.now(UTC) - timedelta(hours=25))
        org = Org.objects.create(name="Org", slug="org", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)

        cleanup_orphaned_org_accounts()
        assert User.objects.filter(id=user.id).exists()

    def test_keeps_personal_account(self):
        user = User.objects.create_user(
            email="personal@example.com",
            full_name="Personal",
            account_type=AccountType.PERSONAL,
        )
        User.objects.filter(id=user.id).update(created_at=datetime.now(UTC) - timedelta(hours=25))
        cleanup_orphaned_org_accounts()
        assert User.objects.filter(id=user.id).exists()

    def test_noop_when_no_orphans(self):
        """No error when there are no orphaned accounts."""
        cleanup_orphaned_org_accounts()
