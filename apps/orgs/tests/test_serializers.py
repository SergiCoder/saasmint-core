"""Tests for orgs serializers."""

from __future__ import annotations

import pytest

from apps.orgs.models import OrgRole
from apps.orgs.serializers import (
    AddMemberSerializer,
    CreateOrgSerializer,
    OrgMemberSerializer,
    OrgSerializer,
    UpdateMemberSerializer,
    UpdateOrgSerializer,
)


@pytest.mark.django_db
class TestOrgSerializer:
    def test_serializes_fields(self, org):
        data = OrgSerializer(org).data
        assert data["id"] == str(org.id)
        assert data["name"] == "Test Org"
        assert data["slug"] == "test-org"
        assert "created_at" in data

    def test_all_fields_read_only(self):
        assert set(OrgSerializer.Meta.read_only_fields) == set(OrgSerializer.Meta.fields)


@pytest.mark.django_db
class TestCreateOrgSerializer:
    def test_valid_data(self):
        ser = CreateOrgSerializer(data={"name": "New Org", "slug": "new-org"})
        assert ser.is_valid(), ser.errors

    def test_missing_required_fields(self):
        ser = CreateOrgSerializer(data={})
        assert not ser.is_valid()
        assert "name" in ser.errors
        assert "slug" in ser.errors

    def test_logo_url_optional(self):
        ser = CreateOrgSerializer(
            data={"name": "Org", "slug": "org-slug", "logo_url": "https://example.com/logo.png"}
        )
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["logo_url"] == "https://example.com/logo.png"

    def test_logo_url_defaults_to_none(self):
        ser = CreateOrgSerializer(data={"name": "Org", "slug": "org-slug"})
        ser.is_valid()
        assert ser.validated_data["logo_url"] is None

    def test_duplicate_slug_rejected(self, org):
        ser = CreateOrgSerializer(data={"name": "Dup", "slug": "test-org"})
        assert not ser.is_valid()
        assert "slug" in ser.errors

    def test_slug_validation_allows_deleted_org_slug(self, org):
        from datetime import UTC, datetime

        org.deleted_at = datetime.now(UTC)
        org.save(update_fields=["deleted_at"])
        ser = CreateOrgSerializer(data={"name": "Reuse", "slug": "test-org"})
        assert ser.is_valid(), ser.errors

    def test_invalid_slug_format(self):
        ser = CreateOrgSerializer(data={"name": "Org", "slug": "invalid slug with spaces"})
        assert not ser.is_valid()
        assert "slug" in ser.errors


class TestUpdateOrgSerializer:
    def test_valid_name_only(self):
        ser = UpdateOrgSerializer(data={"name": "Updated"})
        assert ser.is_valid(), ser.errors

    def test_valid_logo_url_only(self):
        ser = UpdateOrgSerializer(data={"logo_url": "https://example.com/new.png"})
        assert ser.is_valid(), ser.errors

    def test_all_fields_optional(self):
        ser = UpdateOrgSerializer(data={})
        assert ser.is_valid(), ser.errors

    def test_logo_url_nullable(self):
        ser = UpdateOrgSerializer(data={"logo_url": None})
        assert ser.is_valid(), ser.errors


@pytest.mark.django_db
class TestOrgMemberSerializer:
    def test_serializes_fields(self, owner_membership):
        data = OrgMemberSerializer(owner_membership).data
        assert data["id"] == str(owner_membership.id)
        assert data["role"] == "owner"
        assert data["is_billing"] is False
        assert "joined_at" in data

    def test_all_fields_read_only(self):
        assert set(OrgMemberSerializer.Meta.read_only_fields) == set(
            OrgMemberSerializer.Meta.fields
        )


class TestAddMemberSerializer:
    def test_valid_data(self):
        from uuid import uuid4

        ser = AddMemberSerializer(data={"user_id": str(uuid4()), "role": "member"})
        assert ser.is_valid(), ser.errors

    def test_missing_user_id(self):
        ser = AddMemberSerializer(data={"role": "member"})
        assert not ser.is_valid()
        assert "user_id" in ser.errors

    def test_default_role(self):
        from uuid import uuid4

        ser = AddMemberSerializer(data={"user_id": str(uuid4())})
        ser.is_valid()
        assert ser.validated_data["role"] == OrgRole.MEMBER

    def test_default_is_billing_false(self):
        from uuid import uuid4

        ser = AddMemberSerializer(data={"user_id": str(uuid4())})
        ser.is_valid()
        assert ser.validated_data["is_billing"] is False

    def test_invalid_role_rejected(self):
        from uuid import uuid4

        ser = AddMemberSerializer(data={"user_id": str(uuid4()), "role": "superadmin"})
        assert not ser.is_valid()
        assert "role" in ser.errors


class TestUpdateMemberSerializer:
    def test_valid_role(self):
        ser = UpdateMemberSerializer(data={"role": "admin"})
        assert ser.is_valid(), ser.errors

    def test_valid_is_billing(self):
        ser = UpdateMemberSerializer(data={"is_billing": True})
        assert ser.is_valid(), ser.errors

    def test_all_fields_optional(self):
        ser = UpdateMemberSerializer(data={})
        assert ser.is_valid(), ser.errors

    def test_invalid_role_rejected(self):
        ser = UpdateMemberSerializer(data={"role": "superadmin"})
        assert not ser.is_valid()
        assert "role" in ser.errors


class TestCreateOrgSerializerEdgeCases:
    def test_invalid_logo_url_rejected(self):
        ser = CreateOrgSerializer(data={"name": "Org", "slug": "org-slug", "logo_url": "not-a-url"})
        assert not ser.is_valid()
        assert "logo_url" in ser.errors

    def test_name_max_length_exceeded(self):
        ser = CreateOrgSerializer(data={"name": "X" * 256, "slug": "long-name"})
        assert not ser.is_valid()
        assert "name" in ser.errors

    def test_slug_max_length_exceeded(self):
        ser = CreateOrgSerializer(data={"name": "Org", "slug": "x" * 256})
        assert not ser.is_valid()
        assert "slug" in ser.errors

    def test_logo_url_null_is_valid(self):
        ser = CreateOrgSerializer(data={"name": "Org", "slug": "org-slug", "logo_url": None})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["logo_url"] is None


class TestUpdateOrgSerializerEdgeCases:
    def test_invalid_logo_url_rejected(self):
        ser = UpdateOrgSerializer(data={"logo_url": "not-a-url"})
        assert not ser.is_valid()
        assert "logo_url" in ser.errors

    def test_name_max_length_exceeded(self):
        ser = UpdateOrgSerializer(data={"name": "X" * 256})
        assert not ser.is_valid()
        assert "name" in ser.errors


class TestAddMemberSerializerEdgeCases:
    def test_invalid_uuid_rejected(self):
        ser = AddMemberSerializer(data={"user_id": "not-a-uuid", "role": "member"})
        assert not ser.is_valid()
        assert "user_id" in ser.errors

    def test_is_billing_true(self):
        from uuid import uuid4

        ser = AddMemberSerializer(
            data={"user_id": str(uuid4()), "role": "member", "is_billing": True}
        )
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["is_billing"] is True
