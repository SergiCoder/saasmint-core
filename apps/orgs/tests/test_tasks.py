"""Tests for apps.orgs.tasks — Stripe sub-cancel task idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from apps.orgs.models import Invitation, Org, OrgMember, OrgRole
from apps.orgs.tasks import (
    cancel_stripe_subs_task,
    delete_org_on_subscription_cancel_task,
)
from apps.users.models import User


class TestCancelStripeSubsTaskIdempotency:
    """The task can be called more than once for the same sub_id without
    failing (DELETE-then-webhook race, Celery retry after partial success)."""

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_uses_prorate_false(self, mock_cancel):
        cancel_stripe_subs_task(["sub_x"], "org_x")
        mock_cancel.assert_called_once_with("sub_x", prorate=False)

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_swallows_resource_missing(self, mock_cancel):
        mock_cancel.side_effect = stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
            "No such subscription", param="id", code="resource_missing"
        )
        cancel_stripe_subs_task(["sub_already_gone"], "org_xyz")
        mock_cancel.assert_called_once()

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_propagates_other_invalid_request_errors(self, mock_cancel):
        mock_cancel.side_effect = stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
            "Bad request", param="id", code="parameter_unknown"
        )
        with pytest.raises(stripe.InvalidRequestError):
            cancel_stripe_subs_task(["sub_bad"], "org_xyz")

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_propagates_non_invalid_request_stripe_errors(self, mock_cancel):
        """The narrowed except clause must let APIConnectionError, RateLimitError,
        etc. propagate so Celery records the failure for retry/inspection."""
        mock_cancel.side_effect = stripe.APIConnectionError("network down")  # type: ignore[no-untyped-call]
        with pytest.raises(stripe.APIConnectionError):
            cancel_stripe_subs_task(["sub_net"], "org_xyz")

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_processes_each_id_independently(self, mock_cancel):
        mock_cancel.side_effect = [
            stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
                "gone", param="id", code="resource_missing"
            ),
            None,
        ]
        cancel_stripe_subs_task(["sub_gone", "sub_live"], "user:abc")
        assert mock_cancel.call_count == 2

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_continues_loop_after_transient_then_raises(self, mock_cancel):
        """A transient Stripe error on one sub must not skip the remaining ones;
        the failure is still re-raised at the end so Celery records it."""
        mock_cancel.side_effect = [
            stripe.APIConnectionError("boom"),  # type: ignore[no-untyped-call]
            None,
            None,
        ]
        with pytest.raises(stripe.APIConnectionError):
            cancel_stripe_subs_task(["sub_a", "sub_b", "sub_c"], "user:abc")
        assert mock_cancel.call_count == 3

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_continues_loop_after_invalid_request_then_raises(self, mock_cancel):
        mock_cancel.side_effect = [
            stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
                "bad", param="id", code="parameter_unknown"
            ),
            None,
        ]
        with pytest.raises(stripe.InvalidRequestError):
            cancel_stripe_subs_task(["sub_bad", "sub_ok"], "user:abc")
        assert mock_cancel.call_count == 2


@pytest.mark.django_db
class TestDeleteOrgOnSubscriptionCancelTask:
    """Cascade body for the Stripe team-subscription-cancel webhook. The
    webhook callback in ``apps.orgs.services`` is dispatch-only; this task
    does the actual hard-delete cascade off the request path so the webhook
    returns within Stripe's retry window."""

    def test_hard_deletes_org(self):
        user = User.objects.create_user(
            email="cancel-delete@example.com",
            full_name="Cancel Delete",
        )
        org = Org.objects.create(name="Active", slug="active", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)
        org_id = org.id

        delete_org_on_subscription_cancel_task(str(org_id))

        assert not Org.objects.filter(id=org_id).exists()
        assert not OrgMember.objects.filter(org_id=org_id).exists()

    def test_cascades_pending_invitations(self):
        user = User.objects.create_user(
            email="cascadeinv@example.com",
            full_name="Cascade Inv",
        )
        org = Org.objects.create(name="InvOrg", slug="invorg", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)
        Invitation.objects.create(
            org=org,
            email="pending@example.com",
            role=OrgRole.MEMBER,
            token="token-cascade",  # noqa: S106
            invited_by=user,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

        delete_org_on_subscription_cancel_task(str(org.id))

        assert not Invitation.objects.filter(token="token-cascade").exists()  # noqa: S106

    def test_deletes_single_org_member_users(self):
        owner = User.objects.create_user(
            email="cascade-owner@example.com",
            full_name="Owner",
        )
        single_org_member = User.objects.create_user(
            email="single@example.com",
            full_name="Single",
        )
        org = Org.objects.create(name="SingleOrg", slug="singleorg", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER)
        OrgMember.objects.create(org=org, user=single_org_member, role=OrgRole.MEMBER)

        delete_org_on_subscription_cancel_task(str(org.id))

        assert not User.objects.filter(id=owner.id).exists()
        assert not User.objects.filter(id=single_org_member.id).exists()

    def test_preserves_users_with_other_memberships(self):
        owner = User.objects.create_user(
            email="multi-owner@example.com",
            full_name="Owner",
        )
        multi_member = User.objects.create_user(
            email="multi@example.com",
            full_name="Multi",
        )
        org_a = Org.objects.create(name="OrgA", slug="orga", created_by=owner)
        org_b = Org.objects.create(name="OrgB", slug="orgb", created_by=multi_member)
        OrgMember.objects.create(org=org_a, user=owner, role=OrgRole.OWNER)
        OrgMember.objects.create(org=org_a, user=multi_member, role=OrgRole.MEMBER)
        OrgMember.objects.create(org=org_b, user=multi_member, role=OrgRole.OWNER)

        delete_org_on_subscription_cancel_task(str(org_a.id))

        assert User.objects.filter(id=multi_member.id).exists()
        assert OrgMember.objects.filter(user=multi_member, org=org_b).exists()

    def test_missing_org_is_noop(self):
        """DELETE-then-webhook race or duplicate webhook delivery."""
        delete_org_on_subscription_cancel_task(str(uuid4()))

    def test_double_invocation_is_idempotent(self):
        """Stripe webhook redelivery or Celery retry must not raise on the
        second run after the first has hard-deleted the row."""
        user = User.objects.create_user(
            email="idem@example.com",
            full_name="Idem",
        )
        org = Org.objects.create(name="Idem", slug="idem", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)
        org_id = org.id

        delete_org_on_subscription_cancel_task(str(org_id))
        delete_org_on_subscription_cancel_task(str(org_id))

        assert not Org.objects.filter(id=org_id).exists()
