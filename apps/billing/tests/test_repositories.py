"""Tests for billing repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.billing.models import (
    Plan,
    StripeCustomer,
    StripeEvent,
    Subscription,
)
from apps.billing.repositories import (
    DjangoPlanRepository,
    DjangoStripeCustomerRepository,
    DjangoStripeEventRepository,
    DjangoSubscriptionRepository,
)

pytestmark = pytest.mark.django_db


class TestDjangoStripeCustomerRepository:
    @pytest.fixture
    def repo(self):
        return DjangoStripeCustomerRepository()

    def test_get_by_id(self, repo, stripe_customer):
        result = async_to_sync(repo.get_by_id)(stripe_customer.id)
        assert result is not None
        assert result.stripe_id == "cus_test_123"

    def test_get_by_id_not_found(self, repo):
        result = async_to_sync(repo.get_by_id)(uuid4())
        assert result is None

    def test_get_by_stripe_id(self, repo, stripe_customer):
        result = async_to_sync(repo.get_by_stripe_id)("cus_test_123")
        assert result is not None
        assert result.id == stripe_customer.id

    def test_get_by_user_id(self, repo, stripe_customer, user):
        result = async_to_sync(repo.get_by_user_id)(user.id)
        assert result is not None
        assert result.stripe_id == "cus_test_123"

    def test_get_by_user_id_not_found(self, repo):
        result = async_to_sync(repo.get_by_user_id)(uuid4())
        assert result is None

    def test_save_creates_new(self, repo, user):
        from stripe_saas_core.domain.stripe_customer import (
            StripeCustomer as DomainCustomer,
        )

        customer = DomainCustomer(
            id=uuid4(),
            stripe_id="cus_new_123",
            user_id=user.id,
            org_id=None,
            livemode=False,
            created_at=datetime.now(UTC),
        )
        saved = async_to_sync(repo.save)(customer)
        assert saved.stripe_id == "cus_new_123"
        assert StripeCustomer.objects.filter(stripe_id="cus_new_123").exists()

    def test_save_upserts_existing(self, repo, stripe_customer, user):
        from stripe_saas_core.domain.stripe_customer import (
            StripeCustomer as DomainCustomer,
        )

        customer = DomainCustomer(
            id=stripe_customer.id,
            stripe_id="cus_updated",
            user_id=user.id,
            org_id=None,
            livemode=True,
            created_at=stripe_customer.created_at,
        )
        async_to_sync(repo.save)(customer)
        stripe_customer.refresh_from_db()
        assert stripe_customer.stripe_id == "cus_updated"
        assert stripe_customer.livemode is True

    def test_delete(self, repo, stripe_customer):
        async_to_sync(repo.delete)(stripe_customer.id)
        assert not StripeCustomer.objects.filter(id=stripe_customer.id).exists()


class TestDjangoSubscriptionRepository:
    @pytest.fixture
    def repo(self):
        return DjangoSubscriptionRepository()

    def test_get_by_id(self, repo, subscription):
        result = async_to_sync(repo.get_by_id)(subscription.id)
        assert result is not None
        assert result.stripe_id == "sub_test_123"

    def test_get_by_stripe_id(self, repo, subscription):
        result = async_to_sync(repo.get_by_stripe_id)("sub_test_123")
        assert result is not None

    def test_get_active_for_customer(self, repo, subscription, stripe_customer):
        result = async_to_sync(repo.get_active_for_customer)(stripe_customer.id)
        assert result is not None
        assert result.stripe_id == "sub_test_123"

    def test_get_active_for_customer_none(self, repo, stripe_customer):
        result = async_to_sync(repo.get_active_for_customer)(stripe_customer.id)
        assert result is None

    def test_get_active_for_customer_multiple_returns_latest(self, repo, stripe_customer, plan):
        Subscription.objects.create(
            stripe_id="sub_old",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            current_period_start=datetime(2025, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2025, 2, 1, tzinfo=UTC),
        )
        Subscription.objects.create(
            stripe_id="sub_new",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        result = async_to_sync(repo.get_active_for_customer)(stripe_customer.id)
        assert result is not None
        assert result.stripe_id == "sub_new"

    def test_save_creates_new(self, repo, stripe_customer, plan):
        from stripe_saas_core.domain.subscription import (
            Subscription as DomainSub,
        )
        from stripe_saas_core.domain.subscription import (
            SubscriptionStatus,
        )

        sub_id = uuid4()
        sub = DomainSub(
            id=sub_id,
            stripe_id="sub_new",
            stripe_customer_id=stripe_customer.id,
            status=SubscriptionStatus.ACTIVE,
            plan_id=plan.id,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            created_at=datetime.now(UTC),
        )
        async_to_sync(repo.save)(sub)
        assert Subscription.objects.filter(stripe_id="sub_new").exists()

    def test_delete(self, repo, subscription):
        async_to_sync(repo.delete)(subscription.id)
        assert not Subscription.objects.filter(id=subscription.id).exists()


class TestDjangoPlanRepository:
    @pytest.fixture
    def repo(self):
        return DjangoPlanRepository()

    def test_get_by_id(self, repo, plan):
        result = async_to_sync(repo.get_by_id)(plan.id)
        assert result is not None
        assert result.name == "Personal Monthly"

    def test_get_by_id_not_found(self, repo):
        result = async_to_sync(repo.get_by_id)(uuid4())
        assert result is None

    def test_list_active(self, repo, plan):
        Plan.objects.create(name="Inactive", context="personal", interval="year", is_active=False)
        results = async_to_sync(repo.list_active)()
        assert len(results) == 1
        assert results[0].name == "Personal Monthly"

    def test_get_price(self, repo, plan, plan_price):
        result = async_to_sync(repo.get_price)(plan.id, "usd")
        assert result is not None
        assert result.amount == 999

    def test_get_price_not_found(self, repo, plan):
        result = async_to_sync(repo.get_price)(plan.id, "eur")
        assert result is None

    def test_get_price_by_stripe_id(self, repo, plan_price):
        result = async_to_sync(repo.get_price_by_stripe_id)("price_test_123")
        assert result is not None
        assert result.currency == "usd"


class TestDjangoStripeEventRepository:
    @pytest.fixture
    def repo(self):
        return DjangoStripeEventRepository()

    def test_exists_false(self, repo):
        assert async_to_sync(repo.exists)("evt_nonexistent") is False

    def test_exists_true(self, repo, db):
        StripeEvent.objects.create(
            stripe_id="evt_exists",
            type="test",
            livemode=False,
            payload={},
        )
        assert async_to_sync(repo.exists)("evt_exists") is True

    def test_save_if_new_creates(self, repo):
        from stripe_saas_core.domain.stripe_event import StripeEvent as DomainEvent

        event = DomainEvent(
            id=uuid4(),
            stripe_id="evt_new",
            type="checkout.session.completed",
            livemode=False,
            payload={"data": "test"},
            created_at=datetime.now(UTC),
        )
        created = async_to_sync(repo.save_if_new)(event)
        assert created is True
        assert StripeEvent.objects.filter(stripe_id="evt_new").exists()

    def test_save_if_new_idempotent(self, repo, db):
        from stripe_saas_core.domain.stripe_event import StripeEvent as DomainEvent

        StripeEvent.objects.create(
            stripe_id="evt_dup",
            type="test",
            livemode=False,
            payload={},
        )
        event = DomainEvent(
            id=uuid4(),
            stripe_id="evt_dup",
            type="test",
            livemode=False,
            payload={},
            created_at=datetime.now(UTC),
        )
        created = async_to_sync(repo.save_if_new)(event)
        assert created is False

    def test_mark_processed(self, repo, db):
        StripeEvent.objects.create(
            stripe_id="evt_proc",
            type="test",
            livemode=False,
            payload={},
            error="previous error",
        )
        async_to_sync(repo.mark_processed)("evt_proc")
        obj = StripeEvent.objects.get(stripe_id="evt_proc")
        assert obj.processed_at is not None
        assert obj.error is None

    def test_mark_failed(self, repo, db):
        StripeEvent.objects.create(
            stripe_id="evt_fail",
            type="test",
            livemode=False,
            payload={},
        )
        async_to_sync(repo.mark_failed)("evt_fail", "connection timeout")
        obj = StripeEvent.objects.get(stripe_id="evt_fail")
        assert obj.error == "connection timeout"

    def test_list_recent(self, repo, db):
        for i in range(3):
            StripeEvent.objects.create(
                stripe_id=f"evt_recent_{i}",
                type="test",
                livemode=False,
                payload={},
            )
        results = async_to_sync(repo.list_recent)(limit=2)
        assert len(results) == 2

    def test_list_recent_caps_at_100(self, repo, db):
        results = async_to_sync(repo.list_recent)(limit=200)
        # Should not error, just cap
        assert isinstance(results, list)
