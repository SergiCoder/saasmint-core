"""Tests for orgs models."""

from __future__ import annotations

from datetime import UTC, datetime

import django.db.models
import pytest
from django.db import IntegrityError

from apps.orgs.models import Org, OrgMember, OrgRole


@pytest.mark.django_db
class TestOrg:
    def test_str(self, org):
        assert str(org) == "Test Org"

    def test_defaults(self, org):
        assert org.deleted_at is None
        assert org.logo_url is None

    def test_slug_unique(self, user):
        Org.objects.create(name="First", slug="unique-slug", created_by=user)
        with pytest.raises(IntegrityError):
            Org.objects.create(name="Second", slug="unique-slug", created_by=user)

    def test_soft_delete(self, org):
        now = datetime.now(UTC)
        org.deleted_at = now
        org.save(update_fields=["deleted_at"])
        org.refresh_from_db()
        assert org.deleted_at is not None

    def test_created_by_protected(self, org, user):
        """Deleting the user should be blocked because Org references them via PROTECT."""
        with pytest.raises(django.db.models.ProtectedError):
            user.delete()


@pytest.mark.django_db
class TestOrgMember:
    def test_str(self, owner_membership):
        assert "orgowner@example.com" in str(owner_membership)
        assert "Test Org" in str(owner_membership)
        assert "owner" in str(owner_membership)

    def test_default_role(self, org, other_user):
        member = OrgMember.objects.create(org=org, user=other_user)
        assert member.role == OrgRole.MEMBER

    def test_default_is_billing_false(self, org, other_user):
        member = OrgMember.objects.create(org=org, user=other_user)
        assert member.is_billing is False

    def test_unique_constraint_org_user(self, org, user, owner_membership):
        with pytest.raises(IntegrityError):
            OrgMember.objects.create(org=org, user=user, role=OrgRole.MEMBER)

    def test_cascade_delete_org(self, org, owner_membership):
        org_id = org.id
        org.delete()
        assert not OrgMember.objects.filter(org_id=org_id).exists()

    def test_cascade_delete_user(self, org, other_user):
        member = OrgMember.objects.create(org=org, user=other_user)
        member_id = member.id
        other_user.delete()
        assert not OrgMember.objects.filter(id=member_id).exists()


class TestOrgRole:
    def test_choices(self):
        values = [c[0] for c in OrgRole.choices]
        assert "owner" in values
        assert "admin" in values
        assert "member" in values
