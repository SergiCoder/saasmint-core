"""Shared fixtures and in-memory test doubles for saasmint-core-lib tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import (
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    Subscription,
    SubscriptionStatus,
)
from saasmint_core.domain.user import AccountType, User

NOW = datetime(2024, 1, 1, tzinfo=UTC)


# ── In-memory repository doubles ────────────────────────────────────────────


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, User] = {}

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._store.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._store.values() if u.email == email), None)

    async def get_by_supabase_uid(self, supabase_uid: str) -> User | None:
        return next((u for u in self._store.values() if u.supabase_uid == supabase_uid), None)

    async def save(self, user: User) -> User:
        self._store[user.id] = user
        return user

    async def delete(self, user_id: UUID) -> None:
        self._store.pop(user_id, None)

    async def hard_delete(self, user_id: UUID) -> None:
        self._store.pop(user_id, None)

    async def schedule_deletion(self, user_id: UUID, scheduled_at: datetime) -> None:
        user = self._store.get(user_id)
        if user:
            self._store[user_id] = user.model_copy(update={"scheduled_deletion_at": scheduled_at})

    async def cancel_scheduled_deletion(self, user_id: UUID) -> None:
        user = self._store.get(user_id)
        if user:
            self._store[user_id] = user.model_copy(update={"scheduled_deletion_at": None})

    async def list_pending_deletions(self) -> list[User]:
        now = datetime.now(UTC)
        return [
            u
            for u in self._store.values()
            if u.scheduled_deletion_at is not None and u.scheduled_deletion_at <= now
        ]

    async def list_by_org(self, org_id: UUID) -> list[User]:
        return list(self._store.values())


class InMemoryStripeCustomerRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, StripeCustomer] = {}

    async def get_by_id(self, customer_id: UUID) -> StripeCustomer | None:
        return self._store.get(customer_id)

    async def get_by_stripe_id(self, stripe_id: str) -> StripeCustomer | None:
        return next((c for c in self._store.values() if c.stripe_id == stripe_id), None)

    async def get_by_user_id(self, user_id: UUID) -> StripeCustomer | None:
        return next((c for c in self._store.values() if c.user_id == user_id), None)

    async def get_by_org_id(self, org_id: UUID) -> StripeCustomer | None:
        return next((c for c in self._store.values() if c.org_id == org_id), None)

    async def save(self, customer: StripeCustomer) -> StripeCustomer:
        self._store[customer.id] = customer
        return customer

    async def delete(self, customer_id: UUID) -> None:
        self._store.pop(customer_id, None)


class InMemorySubscriptionRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Subscription] = {}

    async def get_by_id(self, subscription_id: UUID) -> Subscription | None:
        return self._store.get(subscription_id)

    async def get_by_stripe_id(self, stripe_id: str) -> Subscription | None:
        return next((s for s in self._store.values() if s.stripe_id == stripe_id), None)

    async def get_active_for_user(self, user_id: UUID) -> Subscription | None:
        return next(
            (
                s
                for s in self._store.values()
                if s.user_id == user_id
                and s.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
            ),
            None,
        )

    async def get_active_for_customer(self, stripe_customer_id: UUID) -> Subscription | None:
        return next(
            (
                s
                for s in self._store.values()
                if s.stripe_customer_id == stripe_customer_id
                and s.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
            ),
            None,
        )

    async def save(self, subscription: Subscription) -> Subscription:
        self._store[subscription.id] = subscription
        return subscription

    async def delete(self, subscription_id: UUID) -> None:
        self._store.pop(subscription_id, None)


class InMemoryStripeEventRepository:
    def __init__(self) -> None:
        self._store: dict[str, StripeEvent] = {}

    async def save(self, event: StripeEvent) -> StripeEvent:
        self._store[event.stripe_id] = event
        return event

    async def save_if_new(self, event: StripeEvent) -> bool:
        if event.stripe_id in self._store:
            return False
        self._store[event.stripe_id] = event
        return True

    async def mark_processed(self, stripe_id: str) -> None:
        if stripe_id in self._store:
            event = self._store[stripe_id]
            self._store[stripe_id] = event.model_copy(update={"processed_at": datetime.now(UTC)})

    async def mark_failed(self, stripe_id: str, error: str) -> None:
        if stripe_id in self._store:
            event = self._store[stripe_id]
            self._store[stripe_id] = event.model_copy(update={"error": error})

    async def list_recent(self, limit: int = 50) -> list[StripeEvent]:
        return list(self._store.values())[:limit]


class InMemoryPlanRepository:
    def __init__(self) -> None:
        self._plans: dict[UUID, Plan] = {}
        self._prices: dict[UUID, PlanPrice] = {}

    async def get_by_id(self, plan_id: UUID) -> Plan | None:
        return self._plans.get(plan_id)

    async def list_active(self) -> list[Plan]:
        return [p for p in self._plans.values() if p.is_active]

    async def get_price(self, plan_id: UUID, currency: str) -> PlanPrice | None:
        return next(
            (p for p in self._prices.values() if p.plan_id == plan_id and p.currency == currency),
            None,
        )

    async def get_price_by_stripe_id(self, stripe_price_id: str) -> PlanPrice | None:
        return next(
            (p for p in self._prices.values() if p.stripe_price_id == stripe_price_id),
            None,
        )


# ── Pytest fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def user_repo() -> InMemoryUserRepository:
    return InMemoryUserRepository()


@pytest.fixture
def customer_repo() -> InMemoryStripeCustomerRepository:
    return InMemoryStripeCustomerRepository()


@pytest.fixture
def subscription_repo() -> InMemorySubscriptionRepository:
    return InMemorySubscriptionRepository()


@pytest.fixture
def event_repo() -> InMemoryStripeEventRepository:
    return InMemoryStripeEventRepository()


@pytest.fixture
def plan_repo() -> InMemoryPlanRepository:
    return InMemoryPlanRepository()


# ── Factory helpers ──────────────────────────────────────────────────────────


def make_user(**overrides: Any) -> User:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "supabase_uid": "sup_abc123",
        "email": "test@example.com",
        "full_name": "Test User",
        "account_type": AccountType.PERSONAL,
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "is_verified": True,
        "created_at": NOW,
    }
    fields.update(overrides)
    return User(**fields)


def make_stripe_customer(
    user_id: UUID | None = None,
    org_id: UUID | None = None,
    **overrides: Any,
) -> StripeCustomer:
    if user_id is None and org_id is None:
        user_id = uuid4()
    fields: dict[str, Any] = {
        "id": uuid4(),
        "stripe_id": "cus_test123",
        "user_id": user_id,
        "org_id": org_id,
        "livemode": False,
        "created_at": NOW,
    }
    fields.update(overrides)
    return StripeCustomer(**fields)


def make_subscription(**overrides: Any) -> Subscription:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "stripe_id": "sub_test123",
        "stripe_customer_id": uuid4(),
        "status": SubscriptionStatus.ACTIVE,
        "plan_id": uuid4(),
        "quantity": 1,
        "current_period_start": NOW,
        "current_period_end": NOW,
        "created_at": NOW,
    }
    fields.update(overrides)
    return Subscription(**fields)


def make_plan(**overrides: Any) -> Plan:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "name": "Pro",
        "context": PlanContext.PERSONAL,
        "interval": PlanInterval.MONTH,
        "is_active": True,
    }
    fields.update(overrides)
    return Plan(**fields)


def make_plan_price(
    plan_id: UUID,
    stripe_price_id: str = "price_test123",
    **overrides: Any,
) -> PlanPrice:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "plan_id": plan_id,
        "stripe_price_id": stripe_price_id,
        "currency": "usd",
        "amount": 999,
    }
    fields.update(overrides)
    return PlanPrice(**fields)
