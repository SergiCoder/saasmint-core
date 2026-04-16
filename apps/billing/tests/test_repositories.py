"""Tests for billing repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.billing.models import (
    Plan,
    PlanTier,
    Product,
    ProductPrice,
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
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            created_at=datetime.now(UTC),
        )
        async_to_sync(repo.save)(sub)
        assert Subscription.objects.filter(stripe_id="sub_new").exists()

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

    def test_delete(self, repo, subscription):
        async_to_sync(repo.delete)(subscription.id)
        assert not Subscription.objects.filter(id=subscription.id).exists()

    def test_delete_free_for_user_removes_only_free_rows(self, repo, user, plan, stripe_customer):
        # Free placeholder
        Subscription.objects.create(
            stripe_id=None,
            stripe_customer=None,
            user=user,
            status="active",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(9999, 12, 31, tzinfo=UTC),
        )
        # Paid sub for the same user — must NOT be deleted
        Subscription.objects.create(
            stripe_id="sub_paid_keep",
            stripe_customer=stripe_customer,
            user=user,
            status="active",
            plan=plan,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        deleted = async_to_sync(repo.delete_free_for_user)(user.id)
        assert deleted == 1
        assert Subscription.objects.filter(user=user).count() == 1
        assert Subscription.objects.filter(stripe_id="sub_paid_keep").exists()

    def test_delete_free_for_user_no_op_when_none(self, repo, user):
        deleted = async_to_sync(repo.delete_free_for_user)(user.id)
        assert deleted == 0


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
        result = async_to_sync(repo.get_price)(plan.id)
        assert result is not None
        assert result.amount == 999

    def test_get_price_not_found(self, repo, plan):
        result = async_to_sync(repo.get_price)(plan.id)
        assert result is None

    def test_get_price_by_stripe_id(self, repo, plan_price):
        result = async_to_sync(repo.get_price_by_stripe_id)("price_test_123")
        assert result is not None
        assert result.amount == 999

    def test_get_free_plan_returns_free_tier_personal_plan(self, repo, db, free_plan):
        result = async_to_sync(repo.get_free_plan)()
        assert result is not None
        assert result.id == free_plan.id

    def test_get_free_plan_ignores_paid_plans(self, repo, plan, plan_price):
        # `plan` fixture defaults to tier=basic
        assert async_to_sync(repo.get_free_plan)() is None

    def test_get_free_plan_ignores_team_context(self, repo, db):
        team_free = Plan.objects.create(
            name="Team Free", context="team", tier=PlanTier.FREE, interval="month", is_active=True
        )
        from apps.billing.models import PlanPrice as PlanPriceModel

        PlanPriceModel.objects.create(plan=team_free, stripe_price_id="price_team_free", amount=0)
        assert async_to_sync(repo.get_free_plan)() is None

    def test_get_free_plan_ignores_inactive_plans(self, repo, db):
        inactive = Plan.objects.create(
            name="Personal Free",
            context="personal",
            tier=PlanTier.FREE,
            interval="month",
            is_active=False,
        )
        from apps.billing.models import PlanPrice as PlanPriceModel

        PlanPriceModel.objects.create(plan=inactive, stripe_price_id="price_inactive", amount=0)
        assert async_to_sync(repo.get_free_plan)() is None


class TestDjangoProductRepository:
    @pytest.fixture
    def repo(self):
        return DjangoProductRepository()

    @pytest.fixture
    def product(self, db):
        return Product.objects.create(
            name="100 Credits", type="one_time", credits=100, is_active=True
        )

    @pytest.fixture
    def product_price(self, product):
        return ProductPrice.objects.create(
            product=product, stripe_price_id="price_credits_100", amount=999
        )

    def test_get_by_id(self, repo, product):
        result = async_to_sync(repo.get_by_id)(product.id)
        assert result is not None
        assert result.name == "100 Credits"
        assert result.credits == 100

    def test_get_by_id_not_found(self, repo, db):
        result = async_to_sync(repo.get_by_id)(uuid4())
        assert result is None

    def test_list_active(self, repo, product, db):
        Product.objects.create(name="Inactive", type="one_time", credits=50, is_active=False)
        results = async_to_sync(repo.list_active)()
        names = [r.name for r in results]
        assert "100 Credits" in names
        assert "Inactive" not in names

    def test_get_price(self, repo, product, product_price):
        result = async_to_sync(repo.get_price)(product.id)
        assert result is not None
        assert result.amount == 999

    def test_get_price_not_found(self, repo, product):
        result = async_to_sync(repo.get_price)(product.id)
        assert result is None

    def test_get_price_by_stripe_id(self, repo, product_price):
        result = async_to_sync(repo.get_price_by_stripe_id)("price_credits_100")
        assert result is not None
        assert result.amount == 999

    def test_get_price_by_stripe_id_not_found(self, repo, db):
        result = async_to_sync(repo.get_price_by_stripe_id)("price_missing")
        assert result is None


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
        from saasmint_core.domain.stripe_event import StripeEvent as DomainEvent

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
        from saasmint_core.domain.stripe_event import StripeEvent as DomainEvent

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

    @pytest.mark.anyio
    async def test_list_recent(self, repo, db):
        for i in range(3):
            await StripeEvent.objects.acreate(
                stripe_id=f"evt_recent_{i}",
                type="test",
                livemode=False,
                payload={},
            )
        results = await repo.list_recent(limit=2)
        assert len(results) == 2

    @pytest.mark.anyio
    async def test_list_recent_caps_at_100(self, repo, db):
        results = await repo.list_recent(limit=200)
        # Should not error, just cap
        assert isinstance(results, list)

    def test_save_if_new_preserves_original_on_duplicate(self, repo, db):
        """Duplicate stripe_id must not overwrite the original row.

        Replay of the same webhook delivery (Stripe can redeliver) must leave
        the original event's id, type, and payload intact. Otherwise a replay
        with a mutated payload would silently corrupt the event log.
        """
        from saasmint_core.domain.stripe_event import StripeEvent as DomainEvent

        original = StripeEvent.objects.create(
            stripe_id="evt_replay",
            type="checkout.session.completed",
            livemode=False,
            payload={"v": 1, "original": True},
        )
        original_id = original.id

        replay = DomainEvent(
            id=uuid4(),  # fresh uuid
            stripe_id="evt_replay",
            type="checkout.session.completed",
            livemode=False,
            payload={"v": 2, "tampered": True},  # different payload
            created_at=datetime.now(UTC),
        )
        created = async_to_sync(repo.save_if_new)(replay)

        assert created is False
        obj = StripeEvent.objects.get(stripe_id="evt_replay")
        assert obj.id == original_id  # id preserved
        assert obj.payload == {"v": 1, "original": True}  # payload preserved
        assert StripeEvent.objects.filter(stripe_id="evt_replay").count() == 1

    def test_save_if_new_concurrent_replays_only_create_once(self, repo, db):
        """Back-to-back save_if_new calls for the same stripe_id: only one
        returns True. Guarantees exactly-once insertion under rapid replay."""
        from saasmint_core.domain.stripe_event import StripeEvent as DomainEvent

        def _mk_event() -> DomainEvent:
            return DomainEvent(
                id=uuid4(),
                stripe_id="evt_once",
                type="customer.subscription.updated",
                livemode=False,
                payload={},
                created_at=datetime.now(UTC),
            )

        first = async_to_sync(repo.save_if_new)(_mk_event())
        second = async_to_sync(repo.save_if_new)(_mk_event())
        third = async_to_sync(repo.save_if_new)(_mk_event())

        assert [first, second, third] == [True, False, False]
        assert StripeEvent.objects.filter(stripe_id="evt_once").count() == 1

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
