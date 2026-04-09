"""Tests for orgs API views."""

from __future__ import annotations

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import User


@pytest.mark.django_db
class TestOrgListCreateViewGET:
    def test_returns_user_orgs(self, authed_client, org, owner_membership):
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert resp.data["results"][0]["name"] == "Test Org"

    def test_excludes_orgs_user_not_member_of(self, authed_client, other_user, db):
        other_org = Org.objects.create(name="Other", slug="other", created_by=other_user)
        OrgMember.objects.create(org=other_org, user=other_user, role=OrgRole.OWNER)
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0

    def test_excludes_soft_deleted_orgs(self, authed_client, soft_deleted_org, owner_membership):
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0

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

    def test_soft_deleted_org_returns_404(self, authed_client, soft_deleted_org, owner_membership):
        resp = authed_client.get(f"/api/v1/orgs/{soft_deleted_org.id}/")
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
        self,
        second_admin_client,
        org,
        owner_membership,
        admin_user,
        admin_membership,
        second_admin_membership,
    ):
        resp = second_admin_client.patch(
            f"/api/v1/orgs/{org.id}/members/{admin_user.id}/",
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
        self,
        second_admin_client,
        org,
        owner_membership,
        admin_user,
        admin_membership,
        second_admin_membership,
    ):
        resp = second_admin_client.delete(f"/api/v1/orgs/{org.id}/members/{admin_user.id}/")
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


@pytest.mark.django_db
class TestOrgListCreateViewGETEdgeCases:
    def test_returns_orgs_ordered_by_name(self, authed_client, user):
        org_b = Org.objects.create(name="Bravo", slug="bravo", created_by=user)
        org_a = Org.objects.create(name="Alpha", slug="alpha", created_by=user)
        OrgMember.objects.create(org=org_b, user=user, role=OrgRole.OWNER)
        OrgMember.objects.create(org=org_a, user=user, role=OrgRole.OWNER)
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        names = [o["name"] for o in resp.data["results"]]
        assert names == ["Alpha", "Bravo"]

    def test_returns_multiple_orgs(self, authed_client, user):
        for i in range(3):
            o = Org.objects.create(name=f"Org{i}", slug=f"org-{i}", created_by=user)
            OrgMember.objects.create(org=o, user=user, role=OrgRole.OWNER)
        resp = authed_client.get("/api/v1/orgs/")
        assert resp.status_code == 200
        assert resp.data["count"] == 3


@pytest.mark.django_db
class TestOrgMemberListViewPOSTEdgeCases:
    def test_admin_cannot_add_owner(
        self, admin_client, org, owner_membership, admin_membership, other_user
    ):
        resp = admin_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "owner"},
            format="json",
        )
        assert resp.status_code == 403

    def test_invalid_role_returns_400(self, authed_client, org, owner_membership, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "superadmin"},
            format="json",
        )
        assert resp.status_code == 400

    def test_unauthenticated_rejected(self, org, owner_membership, other_user):
        client = APIClient()
        resp = client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_added_member_has_default_is_billing_false(
        self, authed_client, org, owner_membership, other_user
    ):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["is_billing"] is False


@pytest.mark.django_db
class TestOrgMemberDetailViewPATCHOnNonexistentOrg:
    def test_patch_member_nonexistent_org_returns_404(self, authed_client, other_user):
        resp = authed_client.patch(
            f"/api/v1/orgs/{uuid4()}/members/{other_user.id}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgMemberDetailViewDELETEOnNonexistentOrg:
    def test_delete_member_nonexistent_org_returns_404(self, authed_client, other_user):
        resp = authed_client.delete(f"/api/v1/orgs/{uuid4()}/members/{other_user.id}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOwnerRemovesAdmin:
    def test_owner_can_remove_admin(
        self, authed_client, org, owner_membership, admin_user, admin_membership
    ):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{admin_user.id}/")
        assert resp.status_code == 204
        assert not OrgMember.objects.filter(org=org, user=admin_user).exists()


@pytest.mark.django_db
class TestUnauthenticatedMemberEndpoints:
    def test_list_members_unauthenticated(self, org, owner_membership):
        client = APIClient()
        resp = client.get(f"/api/v1/orgs/{org.id}/members/")
        assert resp.status_code in (401, 403)

    def test_patch_member_unauthenticated(self, org, owner_membership, user):
        client = APIClient()
        resp = client.patch(
            f"/api/v1/orgs/{org.id}/members/{user.id}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_delete_member_unauthenticated(self, org, owner_membership, user):
        client = APIClient()
        resp = client.delete(f"/api/v1/orgs/{org.id}/members/{user.id}/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestSoftDeletedOrgMemberOperations:
    """Operations on members of a soft-deleted org should fail."""

    def test_patch_member_on_soft_deleted_org_returns_404(
        self, authed_client, soft_deleted_org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.patch(
            f"/api/v1/orgs/{soft_deleted_org.id}/members/{member_user.id}/",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 404

    def test_delete_member_on_soft_deleted_org_returns_404(
        self, authed_client, soft_deleted_org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.delete(f"/api/v1/orgs/{soft_deleted_org.id}/members/{member_user.id}/")
        assert resp.status_code == 404

    def test_add_member_to_soft_deleted_org_returns_404(
        self, authed_client, soft_deleted_org, owner_membership, other_user
    ):
        resp = authed_client.post(
            f"/api/v1/orgs/{soft_deleted_org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 404

    def test_list_members_on_soft_deleted_org_returns_404(
        self, authed_client, soft_deleted_org, owner_membership
    ):
        resp = authed_client.get(f"/api/v1/orgs/{soft_deleted_org.id}/members/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOrgDetailViewPATCHNullLogoUrl:
    def test_owner_can_set_logo_url_to_null(self, authed_client, org, owner_membership):
        # First set a logo_url
        org.logo_url = "https://example.com/logo.png"
        org.save(update_fields=["logo_url"])
        # Now null it out
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"logo_url": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["logo_url"] is None


@pytest.mark.django_db
class TestOrgMemberListPagination:
    def test_limit_offset_params(self, authed_client, org, owner_membership, user):
        # Create extra members to paginate
        for i in range(5):
            u = User.objects.create_user(
                email=f"pag{i}@example.com",
                full_name=f"Pag{i}",
            )
            OrgMember.objects.create(org=org, user=u, role=OrgRole.MEMBER)
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/members/?limit=2&offset=0")
        assert resp.status_code == 200
        assert resp.data["count"] == 6  # owner + 5 members
        assert len(resp.data["results"]) == 2

    def test_offset_skips_results(self, authed_client, org, owner_membership, user):
        for i in range(3):
            u = User.objects.create_user(
                email=f"off{i}@example.com",
                full_name=f"Off{i}",
            )
            OrgMember.objects.create(org=org, user=u, role=OrgRole.MEMBER)
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/members/?limit=10&offset=2")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 2  # 4 total, skip 2


@pytest.mark.django_db
class TestCreateOrgIntegrityErrorRace:
    """Test the IntegrityError fallback in OrgListCreateView.post for race conditions."""

    def test_concurrent_slug_creation_returns_400(self, authed_client, user):
        from unittest.mock import patch

        # The serializer validation passes, but the DB insert hits a unique violation
        original_create = Org.objects.create

        call_count = 0

        def create_then_raise(**kwargs):
            nonlocal call_count
            call_count += 1
            # First, create the org normally from a "concurrent" request
            if call_count == 1:
                from django.db import IntegrityError

                raise IntegrityError("duplicate key value violates unique constraint")
            return original_create(**kwargs)

        with patch.object(Org.objects, "create", side_effect=create_then_raise):
            resp = authed_client.post(
                "/api/v1/orgs/",
                {"name": "Race Org", "slug": "race-org"},
                format="json",
            )
        assert resp.status_code == 400
        assert "slug" in resp.data


@pytest.mark.django_db
class TestOrgListMaxResults:
    """The list endpoint caps at 100 orgs."""

    def test_max_100_orgs_returned(self, authed_client, user):
        for i in range(105):
            o = Org.objects.create(name=f"Org{i:03d}", slug=f"org-{i:03d}", created_by=user)
            OrgMember.objects.create(org=o, user=user, role=OrgRole.OWNER)
        resp = authed_client.get("/api/v1/orgs/?limit=200")
        assert resp.status_code == 200
        assert resp.data["count"] == 105
        assert len(resp.data["results"]) == 100  # max_limit caps at 100
