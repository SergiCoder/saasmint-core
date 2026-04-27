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


def _action_payload(org_ids: list[str], *, confirm: bool = False) -> dict[str, object]:
    data: dict[str, object] = {
        "action": "delete_org_action",
        "_selected_action": org_ids,
    }
    if confirm:
        data["confirm"] = "yes"
    return data


@pytest.mark.django_db
class TestOrgAdminDeleteAction:
    def test_shows_confirmation_page(self, admin_client_django, org, owner_membership):
        resp = admin_client_django.post(
            "/admin/orgs/org/",
            _action_payload([str(org.id)]),
        )
        assert resp.status_code == 200
        assert b"TestOrg" in resp.content
        assert b"permanently delete" in resp.content

    @patch("apps.orgs.services._cancel_team_subscription")
    def test_confirm_deletes_org_and_hard_deletes_members(
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
        org_id = org.id

        resp = admin_client_django.post(
            "/admin/orgs/org/",
            _action_payload([str(org_id)], confirm=True),
        )
        assert resp.status_code == 302  # redirect back to changelist

        assert not Org.objects.filter(id=org_id).exists()
        assert not User.objects.filter(id=owner_id).exists()
        assert not User.objects.filter(id=member_id).exists()
        assert not OrgMember.objects.filter(org_id=org_id).exists()

    def test_builtin_delete_is_disabled(self, admin_client_django, org, owner_membership):
        """The detail-page Delete button should be blocked (403)."""
        resp = admin_client_django.post(f"/admin/orgs/org/{org.id}/delete/", {"post": "yes"})
        assert resp.status_code == 403
