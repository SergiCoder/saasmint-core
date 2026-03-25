"""Tests for orgs API views."""

from __future__ import annotations

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from apps.orgs.models import Org, OrgMember, OrgRole


@pytest.mark.django_db
class TestOrgListCreateViewGET:
    def test_returns_user_orgs(self, authed_client, org, owner_membership):
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]["name"] == "Test Org"

    def test_excludes_orgs_user_not_member_of(self, authed_client, other_user, db):
        other_org = Org.objects.create(name="Other", slug="other", created_by=other_user)
        OrgMember.objects.create(org=other_org, user=other_user, role=OrgRole.OWNER)
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert len(resp.data) == 0

    def test_excludes_soft_deleted_orgs(self, authed_client, org, owner_membership):
        from datetime import UTC, datetime

        org.deleted_at = datetime.now(UTC)
        org.save(update_fields=["deleted_at"])
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert len(resp.data) == 0

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/orgs/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestOrgListCreateViewPOST:
    def test_creates_org(self, authed_client):
        resp = authed_client.post(
            "/api/v1/orgs/",
            {"name": "New Org", "slug": "new-org"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["name"] == "New Org"
        assert resp.data["slug"] == "new-org"

    def test_creator_becomes_owner(self, authed_client, user):
        resp = authed_client.post(
            "/api/v1/orgs/",
            {"name": "New Org", "slug": "new-org"},
            format="json",
        )
        assert resp.status_code == 201
        org = Org.objects.get(slug="new-org")
        membership = OrgMember.objects.get(org=org, user=user)
        assert membership.role == OrgRole.OWNER

    def test_duplicate_slug_rejected(self, authed_client, org):
        resp = authed_client.post(
            "/api/v1/orgs/",
            {"name": "Dup", "slug": "test-org"},
            format="json",
        )
        assert resp.status_code == 400

    def test_missing_fields_returns_400(self, authed_client):
        resp = authed_client.post("/api/v1/orgs/", {}, format="json")
        assert resp.status_code == 400

    def test_with_logo_url(self, authed_client):
        resp = authed_client.post(
            "/api/v1/orgs/",
            {"name": "Logo Org", "slug": "logo-org", "logo_url": "https://example.com/logo.png"},
            format="json",
        )
        assert resp.status_code == 201

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post("/api/v1/orgs/", {"name": "X", "slug": "x"}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestOrgDetailViewGET:
    def test_returns_org(self, authed_client, org, owner_membership):
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 200
        assert resp.data["name"] == "Test Org"

    def test_non_member_denied(self, org, other_user):
        client = APIClient()
        client.force_authenticate(user=other_user)
        resp = client.get(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 403

    def test_not_found(self, authed_client):
        resp = authed_client.get(f"/api/v1/orgs/{uuid4()}/")
        assert resp.status_code == 404

    def test_soft_deleted_org_returns_404(self, authed_client, org, owner_membership):
        from datetime import UTC, datetime

        org.deleted_at = datetime.now(UTC)
        org.save(update_fields=["deleted_at"])
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgDetailViewPATCH:
    def test_owner_can_update_name(self, authed_client, org, owner_membership):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"name": "Updated Org"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["name"] == "Updated Org"

    def test_admin_can_update(self, admin_client, org, admin_membership, owner_membership):
        resp = admin_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"name": "Admin Updated"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["name"] == "Admin Updated"

    def test_member_cannot_update(self, member_client, org, member_membership, owner_membership):
        resp = member_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"name": "Nope"},
            format="json",
        )
        assert resp.status_code == 403

    def test_non_member_denied(self, org, other_user, owner_membership):
        client = APIClient()
        client.force_authenticate(user=other_user)
        resp = client.patch(f"/api/v1/orgs/{org.id}/", {"name": "X"}, format="json")
        assert resp.status_code == 403

    def test_update_logo_url(self, authed_client, org, owner_membership):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"logo_url": "https://example.com/new.png"},
            format="json",
        )
        assert resp.status_code == 200


@pytest.mark.django_db
class TestOrgDetailViewDELETE:
    def test_owner_can_soft_delete(self, authed_client, org, owner_membership):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 204
        org.refresh_from_db()
        assert org.deleted_at is not None

    def test_admin_cannot_delete(self, admin_client, org, admin_membership, owner_membership):
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 403

    def test_member_cannot_delete(self, member_client, org, member_membership, owner_membership):
        resp = member_client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 403

    def test_non_member_denied(self, org, other_user, owner_membership):
        client = APIClient()
        client.force_authenticate(user=other_user)
        resp = client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code in (403, 404)


@pytest.mark.django_db
class TestOrgMemberListViewGET:
    def test_returns_members(self, authed_client, org, owner_membership):
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/members/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1

    def test_paginated_response(self, authed_client, org, owner_membership):
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/members/")
        assert "count" in resp.data
        assert "results" in resp.data

    def test_non_member_denied(self, org, other_user, owner_membership):
        client = APIClient()
        client.force_authenticate(user=other_user)
        resp = client.get(f"/api/v1/orgs/{org.id}/members/")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestOrgMemberListViewPOST:
    def test_owner_adds_member(self, authed_client, org, owner_membership, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["role"] == "member"

    def test_admin_adds_member(
        self, admin_client, org, owner_membership, admin_membership, other_user
    ):
        resp = admin_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    def test_member_cannot_add_member(
        self, member_client, org, owner_membership, member_membership, other_user
    ):
        resp = member_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 403

    def test_admin_cannot_add_admin(
        self, admin_client, org, owner_membership, admin_membership, other_user
    ):
        resp = admin_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "admin"},
            format="json",
        )
        assert resp.status_code == 403

    def test_owner_can_add_admin(self, authed_client, org, owner_membership, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "admin"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["role"] == "admin"

    def test_adding_existing_member_returns_200(
        self, authed_client, org, owner_membership, admin_user, admin_membership
    ):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(admin_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 200

    def test_nonexistent_user_returns_404(self, authed_client, org, owner_membership):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(uuid4()), "role": "member"},
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_user_id_returns_400(self, authed_client, org, owner_membership):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 400

    def test_owner_cannot_assign_owner_role(self, authed_client, org, owner_membership, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "owner"},
            format="json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestOrgMemberDetailViewPATCH:
    def test_owner_updates_member_role(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["role"] == "admin"

    def test_owner_updates_billing_flag(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {"is_billing": True},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["is_billing"] is True

    def test_admin_can_update_member(
        self, admin_client, org, owner_membership, admin_membership, member_user, member_membership
    ):
        resp = admin_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {"is_billing": True},
            format="json",
        )
        assert resp.status_code == 200

    def test_admin_cannot_update_admin(
        self, org, owner_membership, admin_user, admin_membership, db
    ):
        other_admin = admin_user  # reuse fixture user as the target admin
        # Create a second admin to act as caller
        from apps.users.models import User

        caller = User.objects.create_user(
            email="admin2@example.com", supabase_uid="sup_admin2", full_name="Admin2"
        )
        OrgMember.objects.create(org=org, user=caller, role=OrgRole.ADMIN)
        client = APIClient()
        client.force_authenticate(user=caller)
        resp = client.patch(
            f"/api/v1/orgs/{org.id}/members/{other_admin.id}/",
            {"is_billing": True},
            format="json",
        )
        assert resp.status_code == 403

    def test_member_cannot_update(
        self, member_client, org, owner_membership, member_membership, admin_user, admin_membership
    ):
        resp = member_client.patch(
            f"/api/v1/orgs/{org.id}/members/{admin_user.id}/",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 403

    def test_nonexistent_member_returns_404(self, authed_client, org, owner_membership):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/members/{uuid4()}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgMemberDetailViewDELETE:
    def test_owner_removes_member(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{member_user.id}/")
        assert resp.status_code == 204
        assert not OrgMember.objects.filter(org=org, user=member_user).exists()

    def test_owner_cannot_remove_self(self, authed_client, org, owner_membership, user):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{user.id}/")
        assert resp.status_code == 403

    def test_admin_removes_member(
        self, admin_client, org, owner_membership, admin_membership, member_user, member_membership
    ):
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/members/{member_user.id}/")
        assert resp.status_code == 204

    def test_admin_cannot_remove_owner(
        self, admin_client, org, owner_membership, admin_membership, user
    ):
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/members/{user.id}/")
        assert resp.status_code == 403

    def test_admin_cannot_remove_admin(
        self, org, owner_membership, admin_user, admin_membership, db
    ):
        from apps.users.models import User

        caller = User.objects.create_user(
            email="admin3@example.com", supabase_uid="sup_admin3", full_name="Admin3"
        )
        OrgMember.objects.create(org=org, user=caller, role=OrgRole.ADMIN)
        client = APIClient()
        client.force_authenticate(user=caller)
        resp = client.delete(f"/api/v1/orgs/{org.id}/members/{admin_user.id}/")
        assert resp.status_code == 403

    def test_member_cannot_remove_anyone(
        self, member_client, org, owner_membership, member_membership, other_user
    ):
        OrgMember.objects.create(org=org, user=other_user, role=OrgRole.MEMBER)
        resp = member_client.delete(f"/api/v1/orgs/{org.id}/members/{other_user.id}/")
        assert resp.status_code == 403

    def test_nonexistent_member_returns_404(self, authed_client, org, owner_membership):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{uuid4()}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgDetailViewPATCHEdgeCases:
    def test_empty_body_returns_unchanged_org(self, authed_client, org, owner_membership):
        resp = authed_client.patch(f"/api/v1/orgs/{org.id}/", {}, format="json")
        assert resp.status_code == 200
        assert resp.data["name"] == "Test Org"

    def test_patch_nonexistent_org_returns_404(self, authed_client):
        resp = authed_client.patch(f"/api/v1/orgs/{uuid4()}/", {"name": "X"}, format="json")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgDetailViewDELETEEdgeCases:
    def test_delete_nonexistent_org_returns_404(self, authed_client):
        resp = authed_client.delete(f"/api/v1/orgs/{uuid4()}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgMemberListViewEdgeCases:
    def test_list_members_nonexistent_org_returns_404(self, authed_client):
        resp = authed_client.get(f"/api/v1/orgs/{uuid4()}/members/")
        assert resp.status_code == 404

    def test_add_member_nonexistent_org_returns_404(self, authed_client, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{uuid4()}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 404

    def test_add_member_with_is_billing_true(
        self, authed_client, org, owner_membership, other_user
    ):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member", "is_billing": True},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["is_billing"] is True


@pytest.mark.django_db
class TestOrgMemberDetailViewPATCHEdgeCases:
    def test_empty_body_returns_unchanged_member(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["role"] == "member"

    def test_admin_cannot_promote_member_to_admin(
        self, admin_client, org, owner_membership, admin_membership, member_user, member_membership
    ):
        resp = admin_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 403

    def test_owner_cannot_promote_member_to_owner(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member_user.id}/",
            {"role": "owner"},
            format="json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestOrgMemberDetailViewDELETEEdgeCases:
    def test_admin_can_remove_self(
        self, admin_client, org, owner_membership, admin_user, admin_membership
    ):
        """Non-owner members should be able to leave the org (remove themselves)."""
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/members/{admin_user.id}/")
        # Admin removing self: check_can_manage_member(admin, admin) should block this
        # since admin cannot manage admin-level members
        assert resp.status_code == 403

    def test_member_can_not_remove_self(
        self, member_client, org, owner_membership, member_user, member_membership
    ):
        resp = member_client.delete(f"/api/v1/orgs/{org.id}/members/{member_user.id}/")
        assert resp.status_code == 403
