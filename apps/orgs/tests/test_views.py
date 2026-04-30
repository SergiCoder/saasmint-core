"""Tests for orgs API views."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.orgs.models import Invitation, InvitationStatus, Org, OrgMember, OrgRole
from apps.users.models import User

# ---------------------------------------------------------------------------
# Org List (GET /api/v1/orgs/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrgListViewGET:
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

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/orgs/")
        assert resp.status_code in (401, 403)

    def test_returns_orgs_ordered_by_name(self, authed_client, user):
        # Rule 8 (``uniq_org_owner_per_user``) limits a user to one OWNER role,
        # so the second membership uses ADMIN — the listing endpoint sorts
        # on Org.name regardless of the caller's role.
        org_b = Org.objects.create(name="Bravo", slug="bravo", created_by=user)
        org_a = Org.objects.create(name="Alpha", slug="alpha", created_by=user)
        OrgMember.objects.create(org=org_b, user=user, role=OrgRole.OWNER)
        OrgMember.objects.create(org=org_a, user=user, role=OrgRole.ADMIN)
        resp = authed_client.get("/api/v1/orgs/")
        names = [o["name"] for o in resp.data["results"]]
        assert names == ["Alpha", "Bravo"]

    def test_post_not_allowed(self, authed_client):
        resp = authed_client.post(
            "/api/v1/orgs/",
            {"name": "New Org", "slug": "new-org"},
            format="json",
        )
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Org Detail (GET/PATCH/DELETE /api/v1/orgs/{orgId}/)
# ---------------------------------------------------------------------------


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

    def test_member_cannot_update(self, member_client, org, member_membership, owner_membership):
        resp = member_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"name": "Nope"},
            format="json",
        )
        assert resp.status_code == 403

    def test_empty_body_returns_unchanged_org(self, authed_client, org, owner_membership):
        resp = authed_client.patch(f"/api/v1/orgs/{org.id}/", {}, format="json")
        assert resp.status_code == 200
        assert resp.data["name"] == "Test Org"

    def test_owner_can_set_logo_url_to_null(self, authed_client, org, owner_membership):
        org.logo_url = "https://example.com/logo.png"
        org.save(update_fields=["logo_url"])
        resp = authed_client.patch(
            f"/api/v1/orgs/{org.id}/",
            {"logo_url": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["logo_url"] is None


@pytest.mark.django_db
class TestOrgDetailViewDELETE:
    """Org deletion is restricted to owners via DELETE /api/v1/orgs/{id}/."""

    @patch("apps.orgs.tasks.cancel_stripe_subs_task")
    def test_owner_can_delete_org(self, _mock_cancel, authed_client, org, owner_membership):
        org_id = org.id
        resp = authed_client.delete(f"/api/v1/orgs/{org_id}/")
        assert resp.status_code == 204
        assert not Org.objects.filter(id=org_id).exists()

    @patch("apps.orgs.tasks.cancel_stripe_subs_task")
    def test_owner_delete_cancels_stripe_subscription(
        self, mock_cancel, authed_client, org, owner_membership
    ):
        from django.test import TestCase

        from apps.billing.models import Plan, StripeCustomer, Subscription

        plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        customer = StripeCustomer.objects.create(stripe_id="cus_delete", org=org, livemode=False)
        Subscription.objects.create(
            stripe_id="sub_delete",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=2,
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
        )

        # delete_org dispatches via transaction.on_commit, which is a no-op
        # inside the rolled-back test transaction unless we capture it.
        with TestCase.captureOnCommitCallbacks(execute=True):
            resp = authed_client.delete(f"/api/v1/orgs/{org.id}/")

        assert resp.status_code == 204
        mock_cancel.delay.assert_called_once()
        assert mock_cancel.delay.call_args.args[0] == ["sub_delete"]

    def test_admin_cannot_delete_org(self, admin_client, org, admin_membership):
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 403
        assert Org.objects.filter(id=org.id).exists()

    def test_member_cannot_delete_org(self, member_client, org, member_membership):
        resp = member_client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code == 403
        assert Org.objects.filter(id=org.id).exists()

    def test_non_member_gets_403(self, authed_client, other_user, db):
        """Access helper returns 403 (not 404) for non-members to avoid leaking
        org existence to an authenticated outsider."""
        other_org = Org.objects.create(name="Other", slug="other", created_by=other_user)
        OrgMember.objects.create(org=other_org, user=other_user, role=OrgRole.OWNER)
        resp = authed_client.delete(f"/api/v1/orgs/{other_org.id}/")
        assert resp.status_code == 403
        assert Org.objects.filter(id=other_org.id).exists()

    def test_nonexistent_org_gets_404(self, authed_client):
        resp = authed_client.delete(f"/api/v1/orgs/{uuid4()}/")
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self, org):
        client = APIClient()
        resp = client.delete(f"/api/v1/orgs/{org.id}/")
        assert resp.status_code in (401, 403)

    @patch("apps.orgs.tasks.cancel_stripe_subs_task")
    def test_double_delete_is_idempotent(self, _mock_cancel, authed_client, org, owner_membership):
        """Second DELETE after a successful one returns 404 (org already gone)."""
        org_id = org.id
        first = authed_client.delete(f"/api/v1/orgs/{org_id}/")
        assert first.status_code == 204
        second = authed_client.delete(f"/api/v1/orgs/{org_id}/")
        assert second.status_code == 404


# ---------------------------------------------------------------------------
# Org Members (GET /api/v1/orgs/{orgId}/members/)
# ---------------------------------------------------------------------------


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

    def test_post_not_allowed(self, authed_client, org, owner_membership, other_user):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/members/",
            {"user_id": str(other_user.id), "role": "member"},
            format="json",
        )
        assert resp.status_code == 405

    def test_limit_offset_params(self, authed_client, org, owner_membership, user):
        for i in range(5):
            u = User.objects.create_user(
                email=f"pag{i}@example.com",
                full_name=f"Pag{i}",
            )
            OrgMember.objects.create(org=org, user=u, role=OrgRole.MEMBER)
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/members/?limit=2&offset=0")
        assert resp.status_code == 200
        assert resp.data["count"] == 6
        assert len(resp.data["results"]) == 2


# ---------------------------------------------------------------------------
# Member Detail (PATCH/DELETE /api/v1/orgs/{orgId}/members/{userId}/)
# ---------------------------------------------------------------------------


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
    @patch("apps.orgs.services.decrement_subscription_seats")
    def test_owner_removes_member_and_deletes_account(
        self,
        mock_seats,
        authed_client,
        org,
        owner_membership,
        member_user,
        member_membership,
    ):
        member_id = member_user.id
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{member_user.id}/")
        assert resp.status_code == 204
        assert not OrgMember.objects.filter(org=org, user_id=member_id).exists()
        # Member's user account is hard-deleted
        assert not User.objects.filter(id=member_id).exists()

    def test_cannot_remove_owner(self, authed_client, org, owner_membership, user):
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/members/{user.id}/")
        assert resp.status_code == 403

    @patch("apps.orgs.services.decrement_subscription_seats")
    def test_admin_removes_member(
        self,
        mock_seats,
        admin_client,
        org,
        owner_membership,
        admin_membership,
        member_user,
        member_membership,
    ):
        member_id = member_user.id
        resp = admin_client.delete(f"/api/v1/orgs/{org.id}/members/{member_user.id}/")
        assert resp.status_code == 204
        assert not User.objects.filter(id=member_id).exists()

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


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Transfer Ownership (PUT /api/v1/orgs/{orgId}/owner/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrgTransferOwnershipView:
    def test_owner_transfers_to_admin(
        self, authed_client, org, owner_membership, admin_user, admin_membership, user
    ):
        resp = authed_client.put(
            f"/api/v1/orgs/{org.id}/owner/",
            {"user_id": str(admin_user.id)},
            format="json",
        )
        assert resp.status_code == 200
        admin_membership.refresh_from_db()
        owner_membership.refresh_from_db()
        assert admin_membership.role == OrgRole.OWNER
        assert admin_membership.is_billing is True
        assert owner_membership.role == OrgRole.ADMIN
        assert owner_membership.is_billing is False

    def test_cannot_transfer_to_member(
        self, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.put(
            f"/api/v1/orgs/{org.id}/owner/",
            {"user_id": str(member_user.id)},
            format="json",
        )
        assert resp.status_code == 403

    def test_admin_cannot_transfer(
        self, admin_client, org, owner_membership, admin_membership, member_user, member_membership
    ):
        resp = admin_client.put(
            f"/api/v1/orgs/{org.id}/owner/",
            {"user_id": str(member_user.id)},
            format="json",
        )
        assert resp.status_code == 403

    def test_missing_user_id(self, authed_client, org, owner_membership):
        resp = authed_client.put(
            f"/api/v1/orgs/{org.id}/owner/",
            {},
            format="json",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Invitations (CRUD)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationListCreateView:
    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_owner_creates_invitation(self, mock_email, authed_client, org, owner_membership):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "new@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["email"] == "new@example.com"
        assert resp.data["role"] == "member"
        assert resp.data["status"] == "pending"

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_admin_creates_invitation(
        self, mock_email, admin_client, org, owner_membership, admin_membership
    ):
        resp = admin_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "new@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    def test_member_cannot_create_invitation(
        self, member_client, org, owner_membership, member_membership
    ):
        resp = member_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "new@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 403

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_cannot_invite_existing_user(
        self, mock_email, authed_client, org, owner_membership, member_user, member_membership
    ):
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": member_user.email, "role": "member"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "email_exists"

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_cannot_create_duplicate_pending_invitation(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        mock_email.delay = lambda **kw: None
        Invitation.objects.create(
            org=org,
            email="dupe@example.com",
            role=OrgRole.MEMBER,
            token="token-1",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "dupe@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "invitation_pending"

    def test_list_pending_invitations(self, authed_client, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="pending@example.com",
            role=OrgRole.MEMBER,
            token="token-pending",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        Invitation.objects.create(
            org=org,
            email="accepted@example.com",
            role=OrgRole.MEMBER,
            token="token-accepted",  # noqa: S106
            status=InvitationStatus.ACCEPTED,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.get(f"/api/v1/orgs/{org.id}/invitations/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert resp.data["results"][0]["email"] == "pending@example.com"


@pytest.mark.django_db
class TestInvitationCancelView:
    def test_owner_cancels_invitation(self, authed_client, org, owner_membership, user):
        invitation = Invitation.objects.create(
            org=org,
            email="cancel@example.com",
            role=OrgRole.MEMBER,
            token="token-cancel",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.delete(f"/api/v1/orgs/{org.id}/invitations/{invitation.id}/")
        assert resp.status_code == 204
        invitation.refresh_from_db()
        assert invitation.status == InvitationStatus.CANCELLED

    def test_member_cannot_cancel(
        self, member_client, org, owner_membership, member_membership, user
    ):
        invitation = Invitation.objects.create(
            org=org,
            email="cancel@example.com",
            role=OrgRole.MEMBER,
            token="token-cancel-2",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = member_client.delete(f"/api/v1/orgs/{org.id}/invitations/{invitation.id}/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Invitation Accept / Decline
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationAcceptView:
    def test_accept_invitation_registers_user(self, org, owner_membership, user):
        """Accepting creates an unverified user, joins the org, and prepares a verify email.

        Tokens are intentionally NOT returned — the invite token alone does
        not prove mailbox control, so the invitee must click the verification
        email to activate and sign in.
        """
        from apps.users.models import EmailVerificationToken

        invitation = Invitation.objects.create(
            org=org,
            email="newuser@example.com",
            role=OrgRole.MEMBER,
            token="accept-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/accept-token/accept/",
            {"full_name": "New User", "password": "securepass123"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["org"]["name"] == org.name
        assert resp.data["code"] == "verification_email_sent"
        assert "access_token" not in resp.data
        assert "refresh_token" not in resp.data
        # New user created and added as org member, not yet verified
        new_user = User.objects.get(email="newuser@example.com")
        assert new_user.is_verified is False
        assert OrgMember.objects.filter(org=org, user=new_user).exists()
        invitation.refresh_from_db()
        assert invitation.status == InvitationStatus.ACCEPTED
        # A verification token was created so the invitee can prove mailbox control
        assert EmailVerificationToken.objects.filter(user=new_user).exists()

    def test_accept_rejects_already_registered_email(self, org, owner_membership, user, other_user):
        """Cannot accept if the invited email is already registered."""
        Invitation.objects.create(
            org=org,
            email=other_user.email,
            role=OrgRole.MEMBER,
            token="existing-email-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/existing-email-token/accept/",
            {"full_name": "Other", "password": "securepass123"},
            format="json",
        )
        assert resp.status_code == 409

    def test_expired_invitation_rejected(self, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="expired@example.com",
            role=OrgRole.MEMBER,
            token="expired-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() - timedelta(days=1),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/expired-token/accept/",
            {"full_name": "Expired User", "password": "securepass123"},
            format="json",
        )
        assert resp.status_code == 410

    def test_nonexistent_token_returns_404(self):
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/nonexistent/accept/",
            {"full_name": "Nobody", "password": "securepass123"},
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_registration_data_rejected(self, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="nodata@example.com",
            role=OrgRole.MEMBER,
            token="nodata-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post("/api/v1/invitations/nodata-token/accept/", {}, format="json")
        assert resp.status_code == 400

    def test_short_password_rejected(self, org, owner_membership, user):
        """Passwords shorter than 10 chars fail serializer min_length."""
        Invitation.objects.create(
            org=org,
            email="short@example.com",
            role=OrgRole.MEMBER,
            token="short-pass-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/short-pass-token/accept/",
            {"full_name": "Short Pass", "password": "short1"},
            format="json",
        )
        assert resp.status_code == 400
        assert "password" in resp.data

    def test_common_password_rejected(self, org, owner_membership, user):
        """Passwords on CommonPasswordValidator's blocklist are rejected."""
        Invitation.objects.create(
            org=org,
            email="common@example.com",
            role=OrgRole.MEMBER,
            token="common-pass-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/common-pass-token/accept/",
            {"full_name": "Common Pass", "password": "password123"},
            format="json",
        )
        assert resp.status_code == 400
        assert "password" in resp.data

    def test_numeric_password_rejected(self, org, owner_membership, user):
        """Fully numeric passwords are rejected by NumericPasswordValidator."""
        Invitation.objects.create(
            org=org,
            email="numeric@example.com",
            role=OrgRole.MEMBER,
            token="numeric-pass-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/numeric-pass-token/accept/",
            {"full_name": "Numeric Pass", "password": "1234567890"},
            format="json",
        )
        assert resp.status_code == 400
        assert "password" in resp.data

    def test_accept_succeeds_even_when_seats_exhausted(self, org, owner_membership, user):
        """Accept-time has no seat check — seat enforcement is invite-time only.

        Documents the intentional behavior: if seats are exhausted between
        invite creation and accept, the accept still succeeds. Seats are
        validated at invite-time under a row lock.
        """
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )

        customer = StripeCustomer.objects.create(
            stripe_id="cus_accept_full", org=org, livemode=False
        )
        plan = Plan.objects.create(
            name="Team-accept",
            context=PlanContext.TEAM,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_accept_full", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_accept_full",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=1,  # only 1 seat — already filled by owner
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        Invitation.objects.create(
            org=org,
            email="late-accept@example.com",
            role=OrgRole.MEMBER,
            token="late-accept-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )

        client = APIClient()
        resp = client.post(
            "/api/v1/invitations/late-accept-token/accept/",
            {"full_name": "Late Accept", "password": "securepass123"},
            format="json",
        )
        assert resp.status_code == 201
        assert OrgMember.objects.filter(org=org, user__email="late-accept@example.com").exists()


@pytest.mark.django_db
class TestInvitationDeclineView:
    def test_decline_requires_authentication(self, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="decline@example.com",
            role=OrgRole.MEMBER,
            token="decline-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()  # unauthenticated
        resp = client.post("/api/v1/invitations/decline-token/decline/")
        # SessionAuthentication doesn't emit a WWW-Authenticate challenge, so
        # DRF picks 403; JWT auth (the prod default) would emit 401. Accepting
        # either matches the project-wide pattern used in other unauthenticated
        # rejection tests.
        assert resp.status_code in (401, 403)

    def test_decline_rejects_email_mismatch(self, org, owner_membership, user, authed_client):
        invitation = Invitation.objects.create(
            org=org,
            email="someone-else@example.com",
            role=OrgRole.MEMBER,
            token="decline-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.post("/api/v1/invitations/decline-token/decline/")
        assert resp.status_code == 403
        invitation.refresh_from_db()
        assert invitation.status == InvitationStatus.PENDING

    def test_decline_invitation_as_invitee(self, org, owner_membership, user, authed_client):
        invitation = Invitation.objects.create(
            org=org,
            email=user.email,
            role=OrgRole.MEMBER,
            token="decline-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.post("/api/v1/invitations/decline-token/decline/")
        assert resp.status_code == 204
        invitation.refresh_from_db()
        assert invitation.status == InvitationStatus.DECLINED


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUnauthenticatedAccess:
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


# ---------------------------------------------------------------------------
# Invitation Detail (GET /api/v1/invitations/{token}/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationDetailView:
    def test_get_pending_invitation(self, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="detail@example.com",
            role=OrgRole.MEMBER,
            token="detail-token",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()  # unauthenticated
        resp = client.get("/api/v1/invitations/detail-token/")
        assert resp.status_code == 200
        assert resp.data["email"] == "detail@example.com"
        assert resp.data["org_name"] == org.name

    def test_nonexistent_token_returns_404(self):
        client = APIClient()
        resp = client.get("/api/v1/invitations/nonexistent/")
        assert resp.status_code == 404

    def test_accepted_invitation_returns_404(self, org, owner_membership, user):
        Invitation.objects.create(
            org=org,
            email="done@example.com",
            role=OrgRole.MEMBER,
            token="done-token",  # noqa: S106
            invited_by=user,
            status=InvitationStatus.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=7),
        )
        client = APIClient()
        resp = client.get("/api/v1/invitations/done-token/")
        assert resp.status_code == 404


class TestInvitationThrottleScope:
    """Pin all three token-based invitation endpoints to the `auth` throttle scope.

    The detail/accept endpoints are unauthenticated and leak org metadata;
    decline takes a user-chosen token. All three belong in the same bucket as
    login/register to prevent enumeration or brute force.
    """

    def test_detail_view_uses_auth_throttle(self):
        from apps.orgs.views import InvitationDetailView

        assert InvitationDetailView.throttle_scope == "auth"

    def test_accept_view_uses_auth_throttle(self):
        from apps.orgs.views import InvitationAcceptView

        assert InvitationAcceptView.throttle_scope == "auth"

    def test_decline_view_uses_auth_throttle(self):
        from apps.orgs.views import InvitationDeclineView

        assert InvitationDeclineView.throttle_scope == "auth"


# ---------------------------------------------------------------------------
# Seat limit validation on invitation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationSeatLimit:
    @staticmethod
    def _make_team_sub(org, *, quantity: int, stripe_suffix: str = "a"):
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )

        customer = StripeCustomer.objects.create(
            stripe_id=f"cus_seat_{stripe_suffix}", org=org, livemode=False
        )
        plan = Plan.objects.create(
            name=f"Team-{stripe_suffix}",
            context=PlanContext.TEAM,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(
            plan=plan, stripe_price_id=f"price_seat_{stripe_suffix}", amount=1500
        )
        return Subscription.objects.create(
            stripe_id=f"sub_seat_{stripe_suffix}",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=quantity,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_invitation_rejected_when_seat_limit_reached(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        self._make_team_sub(org, quantity=1, stripe_suffix="limit")
        # org already has 1 member (owner) and sub has quantity=1
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "overflow@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "seat_limit_reached"
        assert "seat limit" in resp.data["detail"].lower()

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_invitation_allowed_one_below_boundary(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        # quantity=2, 1 existing member, 0 pending → 1 seat free, allowed.
        self._make_team_sub(org, quantity=2, stripe_suffix="below")
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "newhire@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_invitation_rejected_exactly_at_boundary(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        # quantity=2, 1 member + 1 pending invitation → at boundary.
        self._make_team_sub(org, quantity=2, stripe_suffix="atboundary")
        Invitation.objects.create(
            org=org,
            email="pending@example.com",
            role=OrgRole.MEMBER,
            token="boundary-pending",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "overflow@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "seat_limit_reached"
        assert "seat limit" in resp.data["detail"].lower()

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_cancelled_invitation_frees_seat(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        # quantity=2, 1 owner + 1 pending → boundary.
        self._make_team_sub(org, quantity=2, stripe_suffix="cancel")
        pending = Invitation.objects.create(
            org=org,
            email="pending@example.com",
            role=OrgRole.MEMBER,
            token="cancel-pending",  # noqa: S106
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        # Cancelling the pending invitation returns a seat.
        pending.status = InvitationStatus.CANCELLED
        pending.save(update_fields=["status"])

        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "newhire@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_declined_and_expired_invitations_do_not_consume_seats(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        self._make_team_sub(org, quantity=2, stripe_suffix="declined")
        Invitation.objects.create(
            org=org,
            email="declined@example.com",
            role=OrgRole.MEMBER,
            token="declined-tok",  # noqa: S106
            invited_by=user,
            status=InvitationStatus.DECLINED,
            expires_at=timezone.now() + timedelta(days=7),
        )
        Invitation.objects.create(
            org=org,
            email="expired@example.com",
            role=OrgRole.MEMBER,
            token="expired-tok",  # noqa: S106
            invited_by=user,
            status=InvitationStatus.EXPIRED,
            expires_at=timezone.now() - timedelta(days=1),
        )
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "newhire@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_no_active_subscription_allows_invitation(
        self, mock_email, authed_client, org, owner_membership
    ):
        # Free-plan org default: no team sub attached. `_validate_seat_limit`
        # returns early and the invitation is allowed (seat enforcement
        # kicks in only once a paid team sub exists).
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "newhire@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201

    @patch("apps.orgs.tasks.send_invitation_email_task.delay")
    def test_personal_subscription_ignored_for_seat_check(
        self, mock_email, authed_client, org, owner_membership, user
    ):
        # A personal sub linked to the owner user (not the org) must not
        # be picked up as the org's team sub.
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )

        personal_cust = StripeCustomer.objects.create(
            stripe_id="cus_personal_only", user=user, livemode=False
        )
        personal_plan = Plan.objects.create(
            name="Personal Basic",
            context=PlanContext.PERSONAL,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(
            plan=personal_plan, stripe_price_id="price_personal_only", amount=999
        )
        Subscription.objects.create(
            stripe_id="sub_personal_only",
            stripe_customer=personal_cust,
            status="active",
            plan=personal_plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        # Org has no sub → invitation allowed.
        resp = authed_client.post(
            f"/api/v1/orgs/{org.id}/invitations/",
            {"email": "newhire@example.com", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201
