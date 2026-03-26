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
        assert "USD" in str(plan_price)
        assert "999" in str(plan_price)

    def test_unique_plan_currency(self, plan, plan_price):
        with pytest.raises(IntegrityError):
            PlanPrice.objects.create(
                plan=plan,
                stripe_price_id="price_dup",
                currency="usd",
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

        user = User.objects.create_user(
            email="constraint_user@example.com", supabase_uid="sup_constraint"
        )
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


class TestActiveSubscriptionStatuses:
    def test_contains_active_and_trialing(self):
        values = [s.value for s in ACTIVE_SUBSCRIPTION_STATUSES]
        assert "active" in values
        assert "trialing" in values

    def test_excludes_canceled(self):
        values = [s.value for s in ACTIVE_SUBSCRIPTION_STATUSES]
        assert "canceled" not in values
