"""Tests for apps.billing.services — free plan assignment."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from saasmint_core.domain.subscription import FREE_SUBSCRIPTION_PERIOD_END

from apps.billing.models import (
    Plan,
    PlanPrice,
    PlanTier,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
)
from apps.billing.services import assign_free_plan
from apps.users.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="free@example.com",
        full_name="Free User",
    )


@pytest.mark.django_db
class TestAssignFreePlan:
    def test_creates_subscription_without_stripe_customer(self, user, free_plan):
        assign_free_plan(user)

        sub = Subscription.objects.get(user=user)
        assert sub.stripe_id is None
        assert sub.stripe_customer is None
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.plan == free_plan
        assert sub.quantity == 1
        assert sub.current_period_end == FREE_SUBSCRIPTION_PERIOD_END

    def test_idempotent_when_subscription_exists(self, user, free_plan):
        assign_free_plan(user)
        assign_free_plan(user)

        assert Subscription.objects.filter(user=user).count() == 1

    def test_idempotent_when_paid_subscription_exists(self, user, free_plan):
        """If user already has a subscription, assign_free_plan is a no-op."""
        customer = StripeCustomer.objects.create(
            stripe_id="cus_real_123", user=user, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_real_123",
            stripe_customer=customer,
            user=user,
            status=SubscriptionStatus.ACTIVE,
            plan=free_plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        assign_free_plan(user)
        assert Subscription.objects.filter(user=user).count() == 1

    def test_skips_when_no_free_plan_exists(self, user, db):
        """When there is no free plan in the DB, assign_free_plan does nothing."""
        assign_free_plan(user)
        assert not Subscription.objects.filter(user=user).exists()

    def test_skips_inactive_free_plan(self, user, db):
        """Inactive free plans are not assigned."""
        plan = Plan.objects.create(
            name="Personal Free",
            context="personal",
            tier=PlanTier.FREE,
            interval="month",
            is_active=False,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_free_usd", amount=0)

        assign_free_plan(user)
        assert not Subscription.objects.filter(user=user).exists()

    def test_does_not_pick_paid_plan(self, user, db):
        """Non-free-tier plans should not be selected as the free plan."""
        paid_plan = Plan.objects.create(
            name="Personal Basic",
            context="personal",
            tier=PlanTier.BASIC,
            interval="month",
            is_active=True,
        )
        PlanPrice.objects.create(plan=paid_plan, stripe_price_id="price_basic_usd", amount=1900)

        assign_free_plan(user)
        assert not Subscription.objects.filter(user=user).exists()


# ---------------------------------------------------------------------------
# Credits: grant_credits_for_session + on_product_checkout_completed
# ---------------------------------------------------------------------------


@pytest.fixture
def org_member(db):
    from apps.users.models import AccountType

    return User.objects.create_user(
        email="owner@example.com",
        full_name="Owner",
        account_type=AccountType.ORG_MEMBER,
    )


@pytest.fixture
def org(org_member):
    from apps.orgs.models import Org, OrgMember, OrgRole

    org = Org.objects.create(name="Credit Org", slug="credit-org", created_by=org_member)
    OrgMember.objects.create(org=org, user=org_member, role=OrgRole.OWNER, is_billing=True)
    return org


@pytest.fixture
def boost_product(db):
    from apps.billing.models import Product, ProductType

    return Product.objects.create(
        name="100 Credits", type=ProductType.ONE_TIME, credits=100, is_active=True
    )


@pytest.mark.django_db
class TestGrantCreditsForSession:
    def test_first_call_grants_credits(self, user):
        from apps.billing.models import CreditBalance, CreditTransaction
        from apps.billing.services import grant_credits_for_session

        granted = grant_credits_for_session(
            stripe_session_id="cs_one", amount=50, reason="purchase:Test", user=user
        )
        assert granted is True
        assert CreditBalance.objects.get(user=user).balance == 50
        assert CreditTransaction.objects.filter(stripe_session_id="cs_one").count() == 1

    def test_duplicate_session_id_is_noop(self, user):
        """Same stripe_session_id must not double-credit — gives us free
        idempotency for duplicate webhook deliveries."""
        from apps.billing.models import CreditBalance, CreditTransaction
        from apps.billing.services import grant_credits_for_session

        assert grant_credits_for_session(
            stripe_session_id="cs_dup", amount=50, reason="purchase:Test", user=user
        ) is True
        assert grant_credits_for_session(
            stripe_session_id="cs_dup", amount=50, reason="purchase:Test", user=user
        ) is False

        assert CreditBalance.objects.get(user=user).balance == 50
        assert CreditTransaction.objects.filter(stripe_session_id="cs_dup").count() == 1

    def test_org_scope_routes_to_org_balance(self, org):
        from apps.billing.models import CreditBalance
        from apps.billing.services import grant_credits_for_session

        granted = grant_credits_for_session(
            stripe_session_id="cs_org", amount=200, reason="purchase:Team", org=org
        )
        assert granted is True
        assert CreditBalance.objects.get(org=org).balance == 200

    def test_rejects_both_user_and_org(self, user, org):
        from apps.billing.services import grant_credits_for_session

        with pytest.raises(ValueError, match="Exactly one"):
            grant_credits_for_session(
                stripe_session_id="cs_bad",
                amount=1,
                reason="x",
                user=user,
                org=org,
            )

    def test_rejects_non_positive_amount(self, user):
        from apps.billing.services import grant_credits_for_session

        with pytest.raises(ValueError, match="positive amount"):
            grant_credits_for_session(
                stripe_session_id="cs_zero", amount=0, reason="x", user=user
            )


@pytest.mark.django_db
class TestOnProductCheckoutCompleted:
    def test_personal_purchase_credits_the_user(self, user, boost_product):
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)(
            "cs_personal", boost_product.id, user.id, None
        )
        assert CreditBalance.objects.get(user=user).balance == boost_product.credits

    def test_team_purchase_credits_the_org(self, org_member, org, boost_product):
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)(
            "cs_team", boost_product.id, org_member.id, org.id
        )
        assert CreditBalance.objects.get(org=org).balance == boost_product.credits
        assert not CreditBalance.objects.filter(user=org_member).exists()

    def test_unknown_product_is_ignored(self, user):
        from uuid import uuid4

        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)("cs_x", uuid4(), user.id, None)
        assert not CreditBalance.objects.filter(user=user).exists()
