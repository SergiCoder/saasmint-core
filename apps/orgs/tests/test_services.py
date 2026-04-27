"""Tests for apps.orgs.services — org lifecycle, slug generation, invitations."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.orgs.services import (
    _cancel_team_subscription,
    _create_org_with_owner,
    decrement_subscription_seats,
    delete_org,
    delete_org_on_subscription_cancel,
    delete_orgs_created_by_user,
    generate_unique_slug,
)
from apps.users.models import AccountType, User

# ---------------------------------------------------------------------------
# generate_unique_slug
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGenerateUniqueSlug:
    def test_simple_name(self):
        slug = generate_unique_slug("My Team")
        assert slug == "my-team"

    def test_strips_special_characters(self):
        slug = generate_unique_slug("Hello @World!")
        assert slug == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        slug = generate_unique_slug("---test---")
        assert slug == "test"

    def test_short_name_falls_back_to_org(self):
        slug = generate_unique_slug("A")
        assert slug == "org"

    def test_empty_name_falls_back_to_org(self):
        slug = generate_unique_slug("!@#")
        assert slug == "org"

    def test_appends_suffix_on_collision(self):
        user = User.objects.create_user(
            email="slug-test@example.com",
            full_name="Slug Test",
            account_type=AccountType.ORG_MEMBER,
        )
        Org.objects.create(name="Taken", slug="taken", created_by=user)
        slug = generate_unique_slug("Taken")
        assert slug == "taken-2"

    def test_reuses_slug_after_hard_delete(self):
        user = User.objects.create_user(
            email="slug-del@example.com",
            full_name="Slug Del",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="Deleted", slug="deleted", created_by=user)
        org.delete()
        slug = generate_unique_slug("Deleted")
        assert slug == "deleted"

    def test_increments_suffix_on_multiple_collisions(self):
        user = User.objects.create_user(
            email="multi@example.com",
            full_name="Multi",
            account_type=AccountType.ORG_MEMBER,
        )
        Org.objects.create(name="Org", slug="org", created_by=user)
        Org.objects.create(name="Org 2", slug="org-2", created_by=user)
        slug = generate_unique_slug("Org")
        assert slug == "org-3"


# ---------------------------------------------------------------------------
# _create_org_with_owner
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateOrgWithOwner:
    def test_creates_org_and_owner_membership(self):
        user = User.objects.create_user(
            email="owner@example.com",
            full_name="Owner",
            account_type=AccountType.ORG_MEMBER,
        )
        org, member = _create_org_with_owner(user, "New Org")
        assert org.name == "New Org"
        assert org.created_by == user
        assert member.role == OrgRole.OWNER
        assert member.is_billing is True

    def test_rejects_personal_account_type(self):
        user = User.objects.create_user(
            email="personal@example.com",
            full_name="Personal",
            account_type=AccountType.PERSONAL,
        )
        with pytest.raises(ValueError, match="account_type=org_member"):
            _create_org_with_owner(user, "Bad Org")

    def test_rebinds_existing_user_scoped_stripe_customer(self) -> None:
        """Team checkout init saves a user-scoped StripeCustomer; the webhook
        handler must re-bind it to the new org rather than INSERTing a
        duplicate (UNIQUE violation on stripe_id)."""
        from apps.billing.models import StripeCustomer

        user = User.objects.create_user(
            email="rebind@example.com",
            full_name="Rebind",
            account_type=AccountType.ORG_MEMBER,
        )
        StripeCustomer.objects.create(stripe_id="cus_rebind", user=user, livemode=False)

        org, member = _create_org_with_owner(
            user, "Rebind Org", stripe_customer_id="cus_rebind", livemode=True
        )

        customer = StripeCustomer.objects.get(stripe_id="cus_rebind")
        assert customer.user_id is None
        assert customer.org_id == org.id
        assert customer.livemode is True
        assert member.role == OrgRole.OWNER

    def test_duplicate_webhook_is_idempotent(self) -> None:
        """A second checkout.session.completed delivery must not raise — it
        should return the org+membership already created on the first call."""
        from apps.billing.models import StripeCustomer

        user = User.objects.create_user(
            email="dup@example.com",
            full_name="Dup",
            account_type=AccountType.ORG_MEMBER,
        )
        StripeCustomer.objects.create(stripe_id="cus_dup", user=user, livemode=False)

        org1, member1 = _create_org_with_owner(
            user, "Dup Org", stripe_customer_id="cus_dup", livemode=False
        )
        org2, member2 = _create_org_with_owner(
            user, "Dup Org", stripe_customer_id="cus_dup", livemode=False
        )

        assert org1.id == org2.id
        assert member1.id == member2.id
        assert Org.objects.filter(name="Dup Org").count() == 1


# ---------------------------------------------------------------------------
# delete_org_on_subscription_cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrgOnSubscriptionCancel:
    """The webhook callback is dispatch-only — the cascade body lives in the
    Celery task and is tested in ``apps.orgs.tests.test_tasks``."""

    @patch("apps.orgs.tasks.delete_org_on_subscription_cancel_task.delay")
    def test_dispatches_task_with_stringified_org_id(self, mock_delay):
        org_id = uuid4()
        async_to_sync(delete_org_on_subscription_cancel)(org_id)
        mock_delay.assert_called_once_with(str(org_id))


# ---------------------------------------------------------------------------
# delete_org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrg:
    @patch("apps.orgs.services._cancel_team_subscription")
    def test_hard_deletes_org_and_members(self, mock_cancel):
        user = User.objects.create_user(
            email="delorg@example.com",
            full_name="Del Org",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="DelOrg", slug="delorg", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER, is_billing=True)
        member = User.objects.create_user(
            email="delmember@example.com",
            full_name="Del Member",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER)
        org_id = org.id
        user_id = user.id
        member_id = member.id

        delete_org(org)

        assert not Org.objects.filter(id=org_id).exists()
        assert not User.objects.filter(id=user_id).exists()
        assert not User.objects.filter(id=member_id).exists()
        assert not OrgMember.objects.filter(org_id=org_id).exists()


# ---------------------------------------------------------------------------
# delete_orgs_created_by_user
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrgsCreatedByUser:
    @patch("apps.orgs.services._cancel_team_subscription")
    def test_deletes_all_active_orgs(self, mock_cancel):
        user = User.objects.create_user(
            email="multiorg@example.com",
            full_name="Multi Org",
            account_type=AccountType.ORG_MEMBER,
        )
        org1 = Org.objects.create(name="Org1", slug="org1", created_by=user)
        OrgMember.objects.create(org=org1, user=user, role=OrgRole.OWNER)
        org2 = Org.objects.create(name="Org2", slug="org2", created_by=user)
        OrgMember.objects.create(org=org2, user=user, role=OrgRole.OWNER)
        org1_id = org1.id
        org2_id = org2.id

        delete_orgs_created_by_user(user.id)

        assert not Org.objects.filter(id=org1_id).exists()
        assert not Org.objects.filter(id=org2_id).exists()


# ---------------------------------------------------------------------------
# decrement_subscription_seats
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDecrementSubscriptionSeats:
    def test_no_stripe_customer_is_noop(self):
        """No error when org has no Stripe customer."""
        decrement_subscription_seats(uuid4())

    @patch("apps.orgs.services.async_to_sync")
    def test_calls_update_seat_count(self, mock_async_to_sync):
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="seats@example.com",
            full_name="Seats",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="Seats Org", slug="seats-org", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)
        customer = StripeCustomer.objects.create(stripe_id="cus_seats", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_seats", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_seats",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=3,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        mock_update = MagicMock()
        mock_async_to_sync.return_value = mock_update

        decrement_subscription_seats(org.id)

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.kwargs["quantity"] == 1  # 1 member (owner)


# ---------------------------------------------------------------------------
# _cancel_team_subscription
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelTeamSubscription:
    def test_no_customer_is_noop(self):
        user = User.objects.create_user(
            email="nocust@example.com",
            full_name="No Cust",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="NoCust", slug="nocust", created_by=user)
        _cancel_team_subscription(org)  # should not raise

    @patch("stripe.Subscription.cancel")
    def test_cancels_stripe_subscription(self, mock_cancel):
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="cancelsub@example.com",
            full_name="Cancel Sub",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="CancelSub", slug="cancelsub", created_by=user)
        customer = StripeCustomer.objects.create(stripe_id="cus_cancel", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_cancel", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_cancel",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        _cancel_team_subscription(org)
        mock_cancel.assert_called_once_with("sub_cancel", prorate=False)

    @patch("stripe.Subscription.cancel", side_effect=Exception("Stripe error"))
    def test_logs_error_on_stripe_failure(self, mock_cancel):
        import stripe

        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        mock_cancel.side_effect = stripe.StripeError("fail")

        user = User.objects.create_user(
            email="failcancel@example.com",
            full_name="Fail Cancel",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(name="FailCancel", slug="failcancel", created_by=user)
        customer = StripeCustomer.objects.create(stripe_id="cus_fail", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_fail", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_fail",
            stripe_customer=customer,
            status="active",
            plan=plan,
            quantity=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        # Should not raise — logs the error
        _cancel_team_subscription(org)
        mock_cancel.assert_called_once()
