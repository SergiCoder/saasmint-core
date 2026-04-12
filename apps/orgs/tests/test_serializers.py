"""Tests for orgs serializers."""

from __future__ import annotations

import pytest

from apps.orgs.models import OrgRole
from apps.orgs.serializers import (
    CreateInvitationSerializer,
    InvitationSerializer,
    OrgMemberSerializer,
    OrgSerializer,
    TransferOwnershipSerializer,
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

    def test_invalid_logo_url_rejected(self):
        ser = UpdateOrgSerializer(data={"logo_url": "not-a-url"})
        assert not ser.is_valid()
        assert "logo_url" in ser.errors

    def test_name_max_length_exceeded(self):
        ser = UpdateOrgSerializer(data={"name": "X" * 256})
        assert not ser.is_valid()
        assert "name" in ser.errors


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


class TestCreateInvitationSerializer:
    def test_valid_data(self):
        ser = CreateInvitationSerializer(data={"email": "test@example.com", "role": "member"})
        assert ser.is_valid(), ser.errors

    def test_default_role(self):
        ser = CreateInvitationSerializer(data={"email": "test@example.com"})
        ser.is_valid()
        assert ser.validated_data["role"] == OrgRole.MEMBER

    def test_admin_role_valid(self):
        ser = CreateInvitationSerializer(data={"email": "test@example.com", "role": "admin"})
        assert ser.is_valid(), ser.errors

    def test_owner_role_rejected(self):
        ser = CreateInvitationSerializer(data={"email": "test@example.com", "role": "owner"})
        assert not ser.is_valid()
        assert "role" in ser.errors

    def test_missing_email(self):
        ser = CreateInvitationSerializer(data={"role": "member"})
        assert not ser.is_valid()
        assert "email" in ser.errors

    def test_invalid_email(self):
        ser = CreateInvitationSerializer(data={"email": "not-an-email", "role": "member"})
        assert not ser.is_valid()
        assert "email" in ser.errors


class TestTransferOwnershipSerializer:
    def test_valid_data(self):
        from uuid import uuid4

        ser = TransferOwnershipSerializer(data={"user_id": str(uuid4())})
        assert ser.is_valid(), ser.errors

    def test_missing_user_id(self):
        ser = TransferOwnershipSerializer(data={})
        assert not ser.is_valid()
        assert "user_id" in ser.errors

    def test_invalid_uuid(self):
        ser = TransferOwnershipSerializer(data={"user_id": "not-a-uuid"})
        assert not ser.is_valid()
        assert "user_id" in ser.errors


@pytest.mark.django_db
class TestInvitationSerializer:
    def test_serializes_fields(self, org, user):
        from datetime import timedelta

        from django.utils import timezone

        from apps.orgs.models import Invitation

        invitation = Invitation.objects.create(
            org=org,
            email="invitee@example.com",
            role=OrgRole.MEMBER,
            token="test-token-123",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        data = InvitationSerializer(invitation).data
        assert data["id"] == str(invitation.id)
        assert data["email"] == "invitee@example.com"
        assert data["role"] == "member"
        assert data["status"] == "pending"
        assert data["invited_by"]["email"] == user.email
        assert "created_at" in data
        assert "expires_at" in data
