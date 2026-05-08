"""Tests for billing repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.billing.models import (
    Plan,
    Product,
    StripeCustomer,
    StripeEvent,
    Subscription,
)
from apps.billing.repositories import (
    DjangoPlanRepository,
    DjangoProductRepository,
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

    def test_get_by_org_id(self, repo, db):
        from apps.orgs.models import Org
        from apps.users.models import User

        owner = User.objects.create_user(email="org_owner@example.com")
        org = Org.objects.create(name="Test Org", slug="test-org-repo", created_by=owner)
        StripeCustomer.objects.create(stripe_id="cus_org_test", org=org, livemode=False)
        result = async_to_sync(repo.get_by_org_id)(org.id)
        assert result is not None
        assert result.stripe_id == "cus_org_test"
        assert result.org_id == org.id

    def test_get_by_org_id_not_found(self, repo):
        result = async_to_sync(repo.get_by_org_id)(uuid4())
        assert result is None

    def test_save_creates_new_for_org(self, repo, db):
        from saasmint_core.domain.stripe_customer import (
            StripeCustomer as DomainCustomer,
        )

        from apps.orgs.models import Org
        from apps.users.models import User

        owner = User.objects.create_user(email="save_org_owner@example.com")
        org = Org.objects.create(name="Save Org", slug="save-org", created_by=owner)
        customer = DomainCustomer(
            id=uuid4(),
            stripe_id="cus_org_save_123",
            user_id=None,
            org_id=org.id,
            livemode=False,
            created_at=datetime.now(UTC),
        )
        saved = async_to_sync(repo.save)(customer)
        assert saved.stripe_id == "cus_org_save_123"
        assert StripeCustomer.objects.filter(stripe_id="cus_org_save_123").exists()
        db_obj = StripeCustomer.objects.get(stripe_id="cus_org_save_123")
        assert db_obj.org_id == org.id
        assert db_obj.user_id is None

    def test_save_creates_new(self, repo, user):
        from saasmint_core.domain.stripe_customer import (
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
        from saasmint_core.domain.stripe_customer import (
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
        from saasmint_core.domain.subscription import (
            Subscription as DomainSub,
        )
        from saasmint_core.domain.subscription import (
            SubscriptionStatus,
        )

        sub_id = uuid4()
        sub = DomainSub(
            id=sub_id,
            stripe_id="sub_new",
            stripe_customer_id=stripe_customer.id,
            status=SubscriptionStatus.ACTIVE,
            plan_id=plan.id,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            created_at=datetime.now(UTC),
        )
        async_to_sync(repo.save)(sub)
        assert Subscription.objects.filter(stripe_id="sub_new").exists()

    def test_save_round_trips_cancel_at(self, repo, stripe_customer, plan):
        """``cancel_at`` survives both directions through the repo so the
        webhook can write a scheduled-cancel timestamp and the API view can
        read it back without losing precision."""
        from saasmint_core.domain.subscription import (
            Subscription as DomainSub,
        )
        from saasmint_core.domain.subscription import (
            SubscriptionStatus,
        )

        cancel_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        sub = DomainSub(
            id=uuid4(),
            stripe_id="sub_with_cancel",
            stripe_customer_id=stripe_customer.id,
            status=SubscriptionStatus.ACTIVE,
            plan_id=plan.id,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            cancel_at=cancel_at,
            created_at=datetime.now(UTC),
        )
        async_to_sync(repo.save)(sub)

        loaded = async_to_sync(repo.get_by_stripe_id)("sub_with_cancel")
        assert loaded is not None
        assert loaded.cancel_at == cancel_at

        # Update path: clearing the field (resume) writes NULL back.
        cleared = sub.model_copy(update={"cancel_at": None})
        async_to_sync(repo.save)(cleared)
        reloaded = async_to_sync(repo.get_by_stripe_id)("sub_with_cancel")
        assert reloaded is not None
        assert reloaded.cancel_at is None

    def test_get_active_for_user_returns_active_subscription(self, repo, subscription, user):
        result = async_to_sync(repo.get_active_for_user)(user.id)
        assert result is not None
        assert result.stripe_id == "sub_test_123"

    def test_get_active_for_user_returns_none_when_no_customer(self, repo, db):
        from uuid import uuid4

        result = async_to_sync(repo.get_active_for_user)(uuid4())
        assert result is None

    def test_get_active_for_user_returns_none_when_only_canceled(self, repo, stripe_customer, plan):
        Subscription.objects.create(
            stripe_id="sub_canceled",
            stripe_customer=stripe_customer,
            status="canceled",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        result = async_to_sync(repo.get_active_for_user)(stripe_customer.user_id)
        assert result is None

    def test_get_active_for_user_returns_latest_when_multiple_active(
        self, repo, stripe_customer, plan
    ):
        Subscription.objects.create(
            stripe_id="sub_older",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            current_period_start=datetime(2025, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2025, 2, 1, tzinfo=UTC),
        )
        Subscription.objects.create(
            stripe_id="sub_newer",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        result = async_to_sync(repo.get_active_for_user)(stripe_customer.user_id)
        assert result is not None
        assert result.stripe_id == "sub_newer"

    def test_get_active_for_user_includes_trialing_status(self, repo, stripe_customer, plan):
        Subscription.objects.create(
            stripe_id="sub_trialing",
            stripe_customer=stripe_customer,
            status="trialing",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        result = async_to_sync(repo.get_active_for_user)(stripe_customer.user_id)
        assert result is not None
        assert result.stripe_id == "sub_trialing"


class TestDjangoPlanRepository:
    @pytest.fixture
    def repo(self):
        return DjangoPlanRepository()

    def test_list_active(self, repo, plan):
        Plan.objects.create(name="Inactive", context="personal", interval="year", is_active=False)
        results = async_to_sync(repo.list_active)()
        assert len(results) == 1
        assert results[0].name == "Personal Monthly"

    def test_get_price_by_stripe_id(self, repo, plan_price):
        result = async_to_sync(repo.get_price_by_stripe_id)("price_test_123")
        assert result is not None
        assert result.amount == 999


class TestDjangoProductRepository:
    @pytest.fixture
    def repo(self):
        return DjangoProductRepository()

    @pytest.fixture
    def product(self, db):
        return Product.objects.create(
            name="100 Credits", type="one_time", credits=100, is_active=True
        )

    def test_list_active(self, repo, product, db):
        Product.objects.create(name="Inactive", type="one_time", credits=50, is_active=False)
        results = async_to_sync(repo.list_active)()
        names = [r.name for r in results]
        assert "100 Credits" in names
        assert "Inactive" not in names


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

    def test_mark_processed_clears_previous_error(self, repo, db):
        """A retry that succeeds must clear the prior error message."""
        StripeEvent.objects.create(
            stripe_id="evt_retry",
            type="test",
            livemode=False,
            payload={},
            error="previous transient failure",
        )
        async_to_sync(repo.mark_processed)("evt_retry")
        obj = StripeEvent.objects.get(stripe_id="evt_retry")
        assert obj.error is None
        assert obj.processed_at is not None

    def test_mark_processed_nonexistent_is_noop(self, repo, db):
        """mark_processed on an unknown stripe_id is a silent no-op (no exception)."""
        async_to_sync(repo.mark_processed)("evt_missing")
        assert not StripeEvent.objects.filter(stripe_id="evt_missing").exists()

    def test_mark_failed_nonexistent_is_noop(self, repo, db):
        async_to_sync(repo.mark_failed)("evt_missing", "boom")
        assert not StripeEvent.objects.filter(stripe_id="evt_missing").exists()

    def test_mark_failed_is_idempotent_on_repeated_calls(self, repo, db):
        """Two failure marks leave the latest message — not duplicated rows."""
        StripeEvent.objects.create(
            stripe_id="evt_fail_idem",
            type="test",
            livemode=False,
            payload={},
        )
        async_to_sync(repo.mark_failed)("evt_fail_idem", "first")
        async_to_sync(repo.mark_failed)("evt_fail_idem", "second")
        obj = StripeEvent.objects.get(stripe_id="evt_fail_idem")
        assert obj.error == "second"
        assert StripeEvent.objects.filter(stripe_id="evt_fail_idem").count() == 1

    def test_save_upsert_overwrites_existing_by_id(self, repo, db):
        """`save` is an upsert by primary key — existing row is overwritten."""
        from saasmint_core.domain.stripe_event import StripeEvent as DomainEvent

        existing_id = uuid4()
        StripeEvent.objects.create(
            id=existing_id,
            stripe_id="evt_upsert",
            type="original",
            livemode=False,
            payload={"old": True},
        )
        domain = DomainEvent(
            id=existing_id,
            stripe_id="evt_upsert",
            type="updated",
            livemode=False,
            payload={"new": True},
            processed_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        async_to_sync(repo.save)(domain)
        obj = StripeEvent.objects.get(id=existing_id)
        assert obj.type == "updated"
        assert obj.payload == {"new": True}
        assert obj.processed_at is not None
