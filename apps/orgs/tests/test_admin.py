"""Tests for the OrgAdmin delete action."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import AccountType, User


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(email="super@example.com")


@pytest.fixture
def admin_client_django(superuser):
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def org_owner(db):
    return User.objects.create_user(
        email="orgowner@example.com",
        full_name="Org Owner",
        account_type=AccountType.ORG_MEMBER,
    )


@pytest.fixture
def org(org_owner):
    return Org.objects.create(name="TestOrg", slug="testorg", created_by=org_owner)


@pytest.fixture
def owner_membership(org, org_owner):
    return OrgMember.objects.create(org=org, user=org_owner, role=OrgRole.OWNER, is_billing=True)


@pytest.fixture
def member(db):
    return User.objects.create_user(
        email="member@example.com",
        full_name="Member",
        account_type=AccountType.ORG_MEMBER,
    )


@pytest.fixture
def member_membership(org, member):
    return OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER)


@pytest.mark.django_db
class TestOrgAdminDeleteAction:
    @patch("apps.orgs.services._cancel_team_subscription")
    def test_delete_action_soft_deletes_org_and_hard_deletes_members(
        self,
        mock_cancel_sub,
        admin_client_django,
        org,
        owner_membership,
        member,
        member_membership,
        org_owner,
    ):
        owner_id = org_owner.id
        member_id = member.id

        resp = admin_client_django.post(
            "/admin/orgs/org/",
            {"action": "delete_org_action", "_selected_action": [str(org.id)]},
        )
        assert resp.status_code == 302  # redirect back to changelist

        org.refresh_from_db()
        assert org.deleted_at is not None
        assert not User.objects.filter(id=owner_id).exists()
        assert not User.objects.filter(id=member_id).exists()
        assert not OrgMember.objects.filter(org=org).exists()

    @patch("apps.orgs.services._cancel_team_subscription")
    def test_delete_action_skips_already_deleted_orgs(
        self, mock_cancel_sub, admin_client_django, org, owner_membership, org_owner
    ):
        from django.utils import timezone

        org.deleted_at = timezone.now()
        org.save(update_fields=["deleted_at"])
        owner_id = org_owner.id

        admin_client_django.post(
            "/admin/orgs/org/",
            {"action": "delete_org_action", "_selected_action": [str(org.id)]},
        )
        # Owner should NOT be deleted since org was already soft-deleted
        assert User.objects.filter(id=owner_id).exists()
