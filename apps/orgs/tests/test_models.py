"""Tests for orgs models."""

from __future__ import annotations

from datetime import UTC, datetime

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

    def test_created_by_set_null_on_delete(self, org, user):
        """Deleting the user sets Org.created_by to NULL (SET_NULL)."""
        user.delete()
        org.refresh_from_db()
        assert org.created_by is None


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

    def test_slug_reusable_after_soft_delete(self, user):
        """Conditional unique allows reusing a slug once the original org is soft-deleted."""
        from datetime import UTC, datetime

        org1 = Org.objects.create(name="First", slug="reuse-slug", created_by=user)
        org1.deleted_at = datetime.now(UTC)
        org1.save(update_fields=["deleted_at"])
        # Should not raise — the slug is free for active orgs
        org2 = Org.objects.create(name="Second", slug="reuse-slug", created_by=user)
        assert org2.slug == "reuse-slug"


class TestOrgRole:
    def test_choices(self):
        values = [c[0] for c in OrgRole.choices]
        assert "owner" in values
        assert "admin" in values
        assert "member" in values


@pytest.mark.django_db
class TestInvitation:
    def test_str(self, org, user):
        from datetime import timedelta

        from django.utils import timezone

        from apps.orgs.models import Invitation

        inv = Invitation.objects.create(
            org=org,
            email="invite@example.com",
            role=OrgRole.MEMBER,
            token="test-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        assert "invite@example.com" in str(inv)
        assert "Test Org" in str(inv)
        assert "pending" in str(inv)

    def test_default_status_is_pending(self, org, user):
        from datetime import timedelta

        from django.utils import timezone

        from apps.orgs.models import Invitation, InvitationStatus

        inv = Invitation.objects.create(
            org=org,
            email="default@example.com",
            role=OrgRole.MEMBER,
            token="default-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        assert inv.status == InvitationStatus.PENDING

    def test_unique_pending_per_org_email(self, org, user):
        from datetime import timedelta

        from django.utils import timezone

        from apps.orgs.models import Invitation

        Invitation.objects.create(
            org=org,
            email="dup@example.com",
            role=OrgRole.MEMBER,
            token="first-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        with pytest.raises(IntegrityError):
            Invitation.objects.create(
                org=org,
                email="dup@example.com",
                role=OrgRole.MEMBER,
                token="second-token",  # noqa: S106
                invited_by=user,
                expires_at=timezone.now() + timedelta(days=7),
            )

    def test_cascade_delete_on_org(self, org, user):
        from datetime import timedelta

        from django.utils import timezone

        from apps.orgs.models import Invitation

        Invitation.objects.create(
            org=org,
            email="cascade@example.com",
            role=OrgRole.MEMBER,
            token="cascade-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        org_id = org.id
        org.delete()
        assert not Invitation.objects.filter(org_id=org_id).exists()

    def test_is_active_default_true(self, org):
        assert org.is_active is True
