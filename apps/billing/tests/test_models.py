"""Tests for billing models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import IntegrityError

from apps.billing.models import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    PlanPrice,
    StripeEvent,
    Subscription,
    SubscriptionStatus,
)


@pytest.mark.django_db
class TestPlan:
    def test_str(self, plan):
        assert str(plan) == "Personal Monthly (month)"

    def test_defaults(self, plan):
        assert plan.is_active is True


@pytest.mark.django_db
class TestPlanPrice:
    def test_str(self, plan_price):
        assert "$9.99" in str(plan_price)

    def test_unique_plan(self, plan, plan_price):
        with pytest.raises(IntegrityError):
            PlanPrice.objects.create(
                plan=plan,
                stripe_price_id="price_dup",
                amount=1999,
            )


@pytest.mark.django_db
class TestStripeCustomer:
    def test_str(self, stripe_customer):
        assert str(stripe_customer) == "cus_test_123"

    def test_constraint_rejects_both_null(self, db):
        """StripeCustomer must have exactly one owner — neither user nor org."""
        from apps.billing.models import StripeCustomer

        with pytest.raises(IntegrityError):
            StripeCustomer.objects.create(
                stripe_id="cus_no_owner",
                livemode=False,
            )

    def test_constraint_rejects_both_set(self, db):
        """StripeCustomer must have exactly one owner — not both user and org."""
        from apps.billing.models import StripeCustomer
        from apps.orgs.models import Org
        from apps.users.models import User

        user = User.objects.create_user(email="constraint_user@example.com")
        org = Org.objects.create(name="Constraint Org", slug="constraint-org", created_by=user)
        with pytest.raises(IntegrityError):
            StripeCustomer.objects.create(
                stripe_id="cus_both_owners",
                user=user,
                org=org,
                livemode=False,
            )


@pytest.mark.django_db
class TestSubscription:
    def test_str(self, subscription):
        assert "sub_test_123" in str(subscription)
        assert "active" in str(subscription)

    def test_default_status(self, stripe_customer, plan):
        sub = Subscription.objects.create(
            stripe_id="sub_default",
            stripe_customer=stripe_customer,
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        assert sub.status == SubscriptionStatus.INCOMPLETE


@pytest.mark.django_db
class TestStripeEvent:
    def test_str(self, db):
        event = StripeEvent.objects.create(
            stripe_id="evt_test_123",
            type="checkout.session.completed",
            livemode=False,
            payload={"id": "evt_test_123"},
        )
        assert "evt_test_123" in str(event)
        assert "checkout.session.completed" in str(event)


@pytest.mark.django_db
class TestPlanFreePlans:
    def test_returns_active_personal_free_plans(self, free_plan):
        from apps.billing.models import Plan

        qs = Plan.free_plans()
        assert qs.count() == 1
        assert qs.first().id == free_plan.id

    def test_excludes_inactive_plans(self, db):
        from apps.billing.models import Plan

        inactive = Plan.objects.create(
            name="Personal Free",
            context="personal",
            tier="free",
            interval="month",
            is_active=False,
        )
        PlanPrice.objects.create(plan=inactive, stripe_price_id="price_inactive_free", amount=0)
        assert Plan.free_plans().count() == 0

    def test_excludes_team_context(self, db):
        from apps.billing.models import Plan

        team_free = Plan.objects.create(
            name="Team Free",
            context="team",
            tier="free",
            interval="month",
            is_active=True,
        )
        PlanPrice.objects.create(plan=team_free, stripe_price_id="price_team_free", amount=0)
        assert Plan.free_plans().count() == 0

    def test_excludes_non_free_tier(self, plan, plan_price):
        from apps.billing.models import Plan

        assert Plan.free_plans().count() == 0


@pytest.mark.django_db
class TestPlanUniqueConstraint:
    def test_duplicate_active_context_tier_interval_rejected(self, db):
        from apps.billing.models import Plan

        Plan.objects.create(
            name="Personal Basic Monthly",
            context="personal",
            tier="basic",
            interval="month",
            is_active=True,
        )
        with pytest.raises(IntegrityError):
            Plan.objects.create(
                name="Personal Basic Monthly v2",
                context="personal",
                tier="basic",
                interval="month",
                is_active=True,
            )

    def test_inactive_duplicates_allowed(self, db):
        from apps.billing.models import Plan

        Plan.objects.create(
            name="Legacy 1",
            context="personal",
            tier="pro",
            interval="year",
            is_active=False,
        )
        Plan.objects.create(
            name="Legacy 2",
            context="personal",
            tier="pro",
            interval="year",
            is_active=False,
        )
        # No error — inactive plans are not constrained


@pytest.mark.django_db
class TestProduct:
    def test_str(self, db):
        from apps.billing.models import Product

        product = Product.objects.create(
            name="100 Credits", type="one_time", credits=100, is_active=True
        )
        assert "100 Credits" in str(product)
        assert "100 credits" in str(product)


@pytest.mark.django_db
class TestProductPrice:
    def test_str(self, db):
        from apps.billing.models import Product, ProductPrice

        product = Product.objects.create(
            name="500 Credits", type="one_time", credits=500, is_active=True
        )
        price = ProductPrice.objects.create(
            product=product, stripe_price_id="price_pp_str", amount=4999
        )
        assert "$49.99" in str(price)


class TestActiveSubscriptionStatuses:
    def test_contains_active_and_trialing(self):
        values = [s.value for s in ACTIVE_SUBSCRIPTION_STATUSES]
        assert "active" in values
        assert "trialing" in values

    def test_excludes_canceled(self):
        values = [s.value for s in ACTIVE_SUBSCRIPTION_STATUSES]
        assert "canceled" not in values
