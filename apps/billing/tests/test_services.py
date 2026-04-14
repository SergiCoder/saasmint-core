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
