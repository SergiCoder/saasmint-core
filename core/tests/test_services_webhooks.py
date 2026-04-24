"""Tests for services/webhooks.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import stripe

from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import Plan, PlanTier, SubscriptionStatus
from saasmint_core.exceptions import WebhookDataError
from saasmint_core.services.webhooks import WebhookRepos, process_stored_event
from tests.conftest import (
    InMemoryPlanRepository,
    InMemoryStripeCustomerRepository,
    InMemoryStripeEventRepository,
    InMemorySubscriptionRepository,
    make_plan,
    make_plan_price,
    make_stripe_customer,
    make_subscription,
)

NOW_TS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


def _make_repos(
    event_repo: InMemoryStripeEventRepository | None = None,
    subscription_repo: InMemorySubscriptionRepository | None = None,
    customer_repo: InMemoryStripeCustomerRepository | None = None,
    plan_repo: InMemoryPlanRepository | None = None,
) -> WebhookRepos:
    return WebhookRepos(
        events=event_repo or InMemoryStripeEventRepository(),
        subscriptions=subscription_repo or InMemorySubscriptionRepository(),
        customers=customer_repo or InMemoryStripeCustomerRepository(),
        plans=plan_repo or InMemoryPlanRepository(),
    )


def _sub_event(
    event_type: str,
    stripe_sub_id: str = "sub_webhook",
    stripe_customer_id: str = "cus_webhook",
    price_id: str = "price_webhook",
    trial_end: int | None = None,
    canceled_at: int | None = None,
    quantity: int = 1,
) -> dict[str, object]:
    return {
        "id": "evt_webhook",
        "type": event_type,
        "livemode": False,
        "data": {
            "object": {
                "id": stripe_sub_id,
                "customer": stripe_customer_id,
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": "si_webhook",
                            "price": {"id": price_id},
                            "quantity": quantity,
                        }
                    ]
                },
                "current_period_start": NOW_TS,
                "current_period_end": NOW_TS + 86400,
                "trial_end": trial_end,
                "canceled_at": canceled_at,
            }
        },
    }


async def _persist(repo: InMemoryStripeEventRepository, event: dict[str, object]) -> str:
    """Seed the event store the way the webhook view would before enqueueing."""
    stripe_id = str(event["id"])
    await repo.save(
        StripeEvent(
            id=uuid4(),
            stripe_id=stripe_id,
            type=str(event["type"]),
            livemode=bool(event["livemode"]),
            payload=event,  # type: ignore[arg-type]  # mirrors webhook view; dict[str, Any] tolerated
            created_at=datetime.now(UTC),
        )
    )
    return stripe_id


# ── process_stored_event: top-level ──────────────────────────────────────────


@pytest.mark.anyio
async def test_new_event_is_marked_processed() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    event = {
        "id": "evt_new",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_new"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    saved = event_repo._store["evt_new"]
    assert saved.processed_at is not None
    assert saved.error is None


@pytest.mark.anyio
async def test_dispatch_failure_marks_event_failed_and_reraises(monkeypatch) -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    event = {
        "id": "evt_fail",
        "type": "customer.subscription.created",
        "livemode": False,
        "data": {"object": {}},
    }
    stripe_id = await _persist(event_repo, event)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("dispatch boom")

    monkeypatch.setattr("saasmint_core.services.webhooks._dispatch", _boom)

    with pytest.raises(RuntimeError, match="dispatch boom"):
        await process_stored_event(event, stripe_id, repos)

    saved = event_repo._store["evt_fail"]
    assert saved.error == "dispatch boom"
    assert saved.processed_at is None


@pytest.mark.anyio
async def test_permanent_error_marks_failed_and_propagates_for_no_retry() -> None:
    """WebhookDataError marks the event failed and surfaces as-is so the
    Celery task can skip retrying."""
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    event = _sub_event("customer.subscription.created", stripe_customer_id="cus_unknown")
    stripe_id = await _persist(event_repo, event)

    with pytest.raises(WebhookDataError, match="Unknown customer"):
        await process_stored_event(event, stripe_id, repos)

    saved = event_repo._store["evt_webhook"]
    assert saved.error is not None
    assert saved.processed_at is None


@pytest.mark.anyio
async def test_transient_error_marks_failed_and_propagates_for_retry(monkeypatch) -> None:
    """Transient errors (StripeError / ConnectionError) mark the event failed
    and surface as-is so the Celery task can retry."""
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    event = {
        "id": "evt_transient",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_transient"}},
    }
    stripe_id = await _persist(event_repo, event)

    async def _flaky(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("temporary blip")

    monkeypatch.setattr("saasmint_core.services.webhooks._dispatch", _flaky)

    with pytest.raises(ConnectionError):
        await process_stored_event(event, stripe_id, repos)

    saved = event_repo._store["evt_transient"]
    assert saved.error == "temporary blip"


# ── _dispatch routing ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_dispatch_subscription_updated() -> None:
    """customer.subscription.updated also routes to _sync_subscription."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_upd")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_upd")
    plan_repo._prices[price.id] = price

    repos = _make_repos(event_repo=event_repo, customer_repo=customer_repo, plan_repo=plan_repo)
    event = _sub_event(
        "customer.subscription.updated", stripe_customer_id="cus_upd", price_id="price_upd"
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)


@pytest.mark.anyio
async def test_dispatch_invoice_payment_succeeded() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = {
        "id": "evt_inv_paid",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_abc"}},
    }
    stripe_id = await _persist(event_repo, event)
    await process_stored_event(event, stripe_id, repos)


@pytest.mark.anyio
async def test_dispatch_invoice_payment_failed() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = {
        "id": "evt_inv_fail",
        "type": "invoice.payment_failed",
        "livemode": False,
        "data": {"object": {"id": "in_fail"}},
    }
    stripe_id = await _persist(event_repo, event)
    await process_stored_event(event, stripe_id, repos)


@pytest.mark.anyio
async def test_dispatch_unknown_event_type() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = {
        "id": "evt_unknown",
        "type": "some.unknown.event",
        "livemode": False,
        "data": {"object": {}},
    }
    stripe_id = await _persist(event_repo, event)
    await process_stored_event(event, stripe_id, repos)


# ── _sync_subscription ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sync_subscription_price_not_found_marks_failed() -> None:
    """Known customer but unknown price → event marked as failed, error raised."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_noprice")
    await customer_repo.save(customer)

    repos = _make_repos(event_repo=event_repo, customer_repo=customer_repo)
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_noprice",
        price_id="price_missing",
    )
    stripe_id = await _persist(event_repo, event)

    with pytest.raises(WebhookDataError, match="Unknown price"):
        await process_stored_event(event, stripe_id, repos)

    saved = event_repo._store["evt_webhook"]
    assert saved.error is not None
    assert saved.processed_at is None


@pytest.mark.anyio
async def test_sync_subscription_creates_new() -> None:
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_new_sub")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_new_sub")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_new_sub",
        price_id="price_new_sub",
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    subs = list(subscription_repo._store.values())
    assert len(subs) == 1
    assert subs[0].stripe_id == "sub_webhook"
    assert subs[0].status == SubscriptionStatus.ACTIVE


@pytest.mark.anyio
async def test_sync_subscription_updates_existing() -> None:
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_upd_sub")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_upd_sub")
    plan_repo._prices[price.id] = price

    existing_sub = make_subscription(
        stripe_id="sub_webhook",
        stripe_customer_id=customer.id,
    )
    await subscription_repo.save(existing_sub)

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.updated",
        stripe_customer_id="cus_upd_sub",
        price_id="price_upd_sub",
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    updated = subscription_repo._store[existing_sub.id]
    assert updated.id == existing_sub.id  # same ID preserved


@pytest.mark.anyio
async def test_sync_subscription_quantity_none_defaults_to_one() -> None:
    """Missing quantity in subscription item defaults to 1."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_qty")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_qty")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_qty",
        price_id="price_qty",
        quantity=None,  # type: ignore[arg-type]  # intentional: testing that None quantity is coerced to default value of 1
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.quantity == 1


@pytest.mark.anyio
async def test_sync_subscription_with_explicit_quantity() -> None:
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_qty5")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_qty5")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_qty5",
        price_id="price_qty5",
        quantity=5,
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.quantity == 5


@pytest.mark.anyio
async def test_sync_subscription_with_canceled_at() -> None:
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_cancel_at")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_cancel_at")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_cancel_at",
        price_id="price_cancel_at",
        canceled_at=NOW_TS,
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.canceled_at is not None


# ── _on_subscription_deleted ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_subscription_deleted_unknown_sub_logs_warning() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = {
        "id": "evt_del_unk",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_unknown"}},
    }
    stripe_id = await _persist(event_repo, event)
    await process_stored_event(event, stripe_id, repos)


@pytest.mark.anyio
async def test_subscription_deleted_marks_canceled() -> None:
    event_repo = InMemoryStripeEventRepository()
    subscription_repo = InMemorySubscriptionRepository()
    sub = make_subscription(stripe_id="sub_to_delete")
    await subscription_repo.save(sub)

    repos = _make_repos(event_repo=event_repo, subscription_repo=subscription_repo)
    event = {
        "id": "evt_del_known",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_to_delete"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    updated = subscription_repo._store[sub.id]
    assert updated.status == SubscriptionStatus.CANCELED
    assert updated.canceled_at is not None
    assert updated.stripe_id == "sub_to_delete"
    assert updated.plan_id == sub.plan_id
    assert updated.stripe_customer_id == sub.stripe_customer_id


# ── upgrade-from-free + auto-fallback-to-free ────────────────────────────────


def _seed_free_plan(plan_repo: InMemoryPlanRepository) -> Plan:
    """Insert an active personal free plan + $0 price into *plan_repo*."""
    free_plan = make_plan(name="Personal Free", tier=PlanTier.FREE)
    plan_repo._plans[free_plan.id] = free_plan
    free_price = make_plan_price(plan_id=free_plan.id, stripe_price_id="price_free", amount=0)
    plan_repo._prices[free_price.id] = free_price
    return free_plan


@pytest.mark.anyio
async def test_upgrade_from_free_removes_orphan_free_subscription() -> None:
    """When a free user upgrades, the free placeholder Subscription is deleted."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()

    free_plan = _seed_free_plan(plan_repo)
    free_sub = make_subscription(
        stripe_id=None,
        stripe_customer_id=None,
        user_id=user_id,
        plan_id=free_plan.id,
    )
    await subscription_repo.save(free_sub)

    customer = make_stripe_customer(user_id=user_id, stripe_id="cus_upgrade")
    await customer_repo.save(customer)

    paid_plan = make_plan(name="Personal Pro")
    plan_repo._plans[paid_plan.id] = paid_plan
    paid_price = make_plan_price(plan_id=paid_plan.id, stripe_price_id="price_paid", amount=1900)
    plan_repo._prices[paid_price.id] = paid_price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_sub_id="sub_paid_new",
        stripe_customer_id="cus_upgrade",
        price_id="price_paid",
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    subs = list(subscription_repo._store.values())
    assert len(subs) == 1
    paid = subs[0]
    assert paid.stripe_id == "sub_paid_new"
    assert paid.user_id == user_id  # mirrored from customer
    assert paid.stripe_customer_id == customer.id
    assert paid.plan_id == paid_plan.id
    # The free placeholder is gone
    assert free_sub.id not in subscription_repo._store


@pytest.mark.anyio
async def test_org_upgrade_does_not_touch_user_free_subs() -> None:
    """Org subscriptions have user_id=None and must not delete unrelated free rows."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    other_user_id = uuid4()
    free_plan = _seed_free_plan(plan_repo)
    other_free_sub = make_subscription(
        stripe_id=None,
        stripe_customer_id=None,
        user_id=other_user_id,
        plan_id=free_plan.id,
    )
    await subscription_repo.save(other_free_sub)

    org_customer = make_stripe_customer(org_id=uuid4(), stripe_id="cus_org")
    await customer_repo.save(org_customer)

    team_plan = make_plan(name="Team Pro")
    plan_repo._plans[team_plan.id] = team_plan
    team_price = make_plan_price(plan_id=team_plan.id, stripe_price_id="price_team", amount=2900)
    plan_repo._prices[team_price.id] = team_price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_sub_id="sub_team_new",
        stripe_customer_id="cus_org",
        price_id="price_team",
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    # Other user's free sub is untouched
    assert other_free_sub.id in subscription_repo._store
    new_team = next(s for s in subscription_repo._store.values() if s.stripe_id == "sub_team_new")
    assert new_team.user_id is None  # org sub


@pytest.mark.anyio
async def test_retry_after_crash_still_prunes_free_subscription() -> None:
    """Regression: if a prior run saved the paid sub but crashed before pruning
    the free row, a retried created/updated event must still prune it.

    Previously the cleanup was guarded by `existing is None`, so a retry would
    see the paid sub already saved and skip the cleanup forever, leaving the
    user with two active subscriptions.
    """
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()

    # Seed: the free placeholder survived from a previous crashed run
    free_plan = _seed_free_plan(plan_repo)
    free_sub = make_subscription(
        stripe_id=None,
        stripe_customer_id=None,
        user_id=user_id,
        plan_id=free_plan.id,
    )
    await subscription_repo.save(free_sub)

    customer = make_stripe_customer(user_id=user_id, stripe_id="cus_retry")
    await customer_repo.save(customer)

    paid_plan = make_plan(name="Personal Pro")
    plan_repo._plans[paid_plan.id] = paid_plan
    paid_price = make_plan_price(
        plan_id=paid_plan.id, stripe_price_id="price_paid_retry", amount=1900
    )
    plan_repo._prices[paid_price.id] = paid_price

    # Seed: the paid sub from the previous (crashed) run is already persisted
    already_saved_paid = make_subscription(
        stripe_id="sub_retry",
        stripe_customer_id=customer.id,
        user_id=user_id,
        plan_id=paid_plan.id,
    )
    await subscription_repo.save(already_saved_paid)

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_sub_id="sub_retry",
        stripe_customer_id="cus_retry",
        price_id="price_paid_retry",
    )
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    subs = list(subscription_repo._store.values())
    assert len(subs) == 1
    assert subs[0].stripe_id == "sub_retry"
    assert free_sub.id not in subscription_repo._store


@pytest.mark.anyio
async def test_paid_cancellation_creates_fresh_free_subscription() -> None:
    """When a personal paid sub is canceled, the user is moved back to free."""
    event_repo = InMemoryStripeEventRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()
    free_plan = _seed_free_plan(plan_repo)

    paid_sub = make_subscription(
        stripe_id="sub_paid_cancel",
        user_id=user_id,
    )
    await subscription_repo.save(paid_sub)

    repos = _make_repos(
        event_repo=event_repo, plan_repo=plan_repo, subscription_repo=subscription_repo
    )
    event = {
        "id": "evt_cancel_fallback",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_paid_cancel"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    # Old paid sub is canceled
    canceled = subscription_repo._store[paid_sub.id]
    assert canceled.status == SubscriptionStatus.CANCELED
    assert canceled.canceled_at is not None

    # A new free sub now exists for the same user
    free_subs = [
        s for s in subscription_repo._store.values() if s.stripe_id is None and s.user_id == user_id
    ]
    assert len(free_subs) == 1
    new_free = free_subs[0]
    assert new_free.status == SubscriptionStatus.ACTIVE
    assert new_free.plan_id == free_plan.id
    assert new_free.stripe_customer_id is None


@pytest.mark.anyio
async def test_org_cancellation_does_not_create_free_subscription() -> None:
    """Org subs (user_id=None) are skipped — there's no team-level free plan."""
    event_repo = InMemoryStripeEventRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()
    _seed_free_plan(plan_repo)

    org_paid_sub = make_subscription(stripe_id="sub_org_cancel", user_id=None)
    await subscription_repo.save(org_paid_sub)

    repos = _make_repos(
        event_repo=event_repo, plan_repo=plan_repo, subscription_repo=subscription_repo
    )
    event = {
        "id": "evt_org_cancel",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_org_cancel"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert subscription_repo._store[org_paid_sub.id].status == SubscriptionStatus.CANCELED
    # No new free subscription created
    assert len(subscription_repo._store) == 1


@pytest.mark.anyio
async def test_cancellation_logs_when_no_free_plan_configured() -> None:
    """Without a configured free plan, cancellation just marks canceled and warns."""
    event_repo = InMemoryStripeEventRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    paid_sub = make_subscription(stripe_id="sub_no_free", user_id=uuid4())
    await subscription_repo.save(paid_sub)

    repos = _make_repos(
        event_repo=event_repo, plan_repo=plan_repo, subscription_repo=subscription_repo
    )
    event = {
        "id": "evt_no_free",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_no_free"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert subscription_repo._store[paid_sub.id].status == SubscriptionStatus.CANCELED
    assert len(subscription_repo._store) == 1  # no fallback created


# ── Basil API: period fields on items ────────────────────────────────────────


@pytest.mark.anyio
async def test_sync_subscription_reads_period_from_items_first() -> None:
    """Stripe API 2024-06+ moved current_period_start/end to subscription items."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_item_period")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_item_period")
    plan_repo._prices[price.id] = price

    item_start_ts = NOW_TS + 1000
    item_end_ts = NOW_TS + 90000

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = {
        "id": "evt_item_period",
        "type": "customer.subscription.created",
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_item_period",
                "customer": "cus_item_period",
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": "si_item",
                            "price": {"id": "price_item_period"},
                            "quantity": 1,
                            "current_period_start": item_start_ts,
                            "current_period_end": item_end_ts,
                        }
                    ]
                },
                # Top-level period fields differ — item values should win.
                "current_period_start": NOW_TS,
                "current_period_end": NOW_TS + 86400,
                "trial_end": None,
                "canceled_at": None,
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    sub = next(iter(subscription_repo._store.values()))
    assert int(sub.current_period_start.timestamp()) == item_start_ts
    assert int(sub.current_period_end.timestamp()) == item_end_ts


@pytest.mark.anyio
async def test_sync_subscription_missing_period_raises_webhook_data_error() -> None:
    """Missing current_period_start/end raises WebhookDataError."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_no_period")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_no_period")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
    )
    event = {
        "id": "evt_no_period",
        "type": "customer.subscription.created",
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_no_period",
                "customer": "cus_no_period",
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": "si_no_period",
                            "price": {"id": "price_no_period"},
                            "quantity": 1,
                            # No current_period_start/end on item or top-level
                        }
                    ]
                },
                "trial_end": None,
                "canceled_at": None,
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    with pytest.raises(WebhookDataError, match="missing current_period"):
        await process_stored_event(event, stripe_id, repos)


@pytest.mark.anyio
async def test_sync_subscription_non_integer_period_raises_webhook_data_error() -> None:
    """Non-integer current_period_* (e.g. a string from a malformed payload)
    surfaces as WebhookDataError instead of a bare ValueError — keeps the
    permanent/transient split in process_stored_event working."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_bad_period")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_bad_period")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        event_repo=event_repo,
        customer_repo=customer_repo,
        plan_repo=plan_repo,
    )
    event = {
        "id": "evt_bad_period",
        "type": "customer.subscription.created",
        "livemode": False,
        "data": {
            "object": {
                "id": "sub_bad_period",
                "customer": "cus_bad_period",
                "status": "active",
                "items": {
                    "data": [
                        {
                            "id": "si_bad_period",
                            "price": {"id": "price_bad_period"},
                            "quantity": 1,
                            "current_period_start": "not-a-number",
                            "current_period_end": NOW_TS + 86400,
                        }
                    ]
                },
                "trial_end": None,
                "canceled_at": None,
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    with pytest.raises(WebhookDataError, match="non-integer current_period"):
        await process_stored_event(event, stripe_id, repos)


# ── Stripe upstream errors ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stripe_error_from_dispatch_propagates_as_transient(monkeypatch) -> None:
    """StripeError raised inside dispatch is caught, marked failed, and re-raised
    so the Celery task can retry."""
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = {
        "id": "evt_stripe_down",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_down"}},
    }
    stripe_id = await _persist(event_repo, event)

    async def _flaky(*_args: object, **_kwargs: object) -> None:
        raise stripe.StripeError("api down")

    monkeypatch.setattr("saasmint_core.services.webhooks._dispatch", _flaky)

    with pytest.raises(stripe.StripeError):
        await process_stored_event(event, stripe_id, repos)

    assert event_repo._store["evt_stripe_down"].error == "api down"


# ── checkout.session.completed: mode routing + product checkout ──────────────


def _payment_checkout_event(
    session_id: str = "cs_prod_001",
    product_id: str = "c2faa000-0000-0000-0000-000000000001",
    user_id: str = "a1111111-0000-0000-0000-000000000000",
    org_id: str | None = None,
) -> dict[str, Any]:
    """Build a checkout.session.completed event with mode=payment."""
    metadata: dict[str, str] = {"product_id": product_id}
    if org_id is not None:
        metadata["org_id"] = org_id
    return {
        "id": "evt_prod_checkout",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": session_id,
                "mode": "payment",
                "client_reference_id": user_id,
                "metadata": metadata,
            }
        },
    }


@pytest.mark.anyio
async def test_payment_mode_routes_to_product_callback() -> None:
    """A mode=payment session must invoke on_product_checkout_completed
    (not on_team_checkout_completed, which is for subscription mode only)."""
    event_repo = InMemoryStripeEventRepository()
    calls: list[tuple[str, UUID, UUID, UUID | None]] = []
    team_calls: list[tuple[object, ...]] = []

    async def _on_product(sid: str, pid: UUID, uid: UUID, oid: UUID | None) -> None:
        calls.append((sid, pid, uid, oid))

    async def _on_team(*args: object) -> None:
        team_calls.append(args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_product_checkout_completed=_on_product,
        on_team_checkout_completed=_on_team,
    )
    event = _payment_checkout_event()
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert len(calls) == 1
    session_id, product_id, user_id, org_id = calls[0]
    assert session_id == "cs_prod_001"
    assert str(product_id) == "c2faa000-0000-0000-0000-000000000001"
    assert str(user_id) == "a1111111-0000-0000-0000-000000000000"
    assert org_id is None
    assert team_calls == []


@pytest.mark.anyio
async def test_payment_mode_passes_org_id_when_present() -> None:
    event_repo = InMemoryStripeEventRepository()
    calls: list[tuple[str, UUID, UUID, UUID | None]] = []

    async def _on_product(sid: str, pid: UUID, uid: UUID, oid: UUID | None) -> None:
        calls.append((sid, pid, uid, oid))

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_product_checkout_completed=_on_product,
    )
    event = _payment_checkout_event(org_id="b2222222-0000-0000-0000-000000000000")
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert len(calls) == 1
    assert str(calls[0][3]) == "b2222222-0000-0000-0000-000000000000"


@pytest.mark.anyio
async def test_payment_mode_missing_product_id_is_noop() -> None:
    """Missing product_id metadata logs and returns — no callback invoked,
    event still marked processed (permanent parse failure, not transient)."""
    event_repo = InMemoryStripeEventRepository()
    calls: list[object] = []

    async def _on_product(*_args: object) -> None:
        calls.append(_args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_product_checkout_completed=_on_product,
    )
    event: dict[str, Any] = {
        "id": "evt_prod_missing_pid",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_no_pid",
                "mode": "payment",
                "client_reference_id": "a1111111-0000-0000-0000-000000000000",
                "metadata": {},
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert calls == []
    assert event_repo._store[stripe_id].processed_at is not None


@pytest.mark.anyio
async def test_payment_mode_malformed_uuid_is_noop() -> None:
    """Malformed UUIDs are logged and swallowed — the event is marked
    processed (no transient error)."""
    event_repo = InMemoryStripeEventRepository()
    calls: list[object] = []

    async def _on_product(*_args: object) -> None:
        calls.append(_args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_product_checkout_completed=_on_product,
    )
    event: dict[str, Any] = {
        "id": "evt_prod_bad_uuid",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_bad_uuid",
                "mode": "payment",
                "client_reference_id": "not-a-uuid",
                "metadata": {"product_id": "also-not-a-uuid"},
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert calls == []


@pytest.mark.anyio
async def test_payment_mode_without_callback_is_noop() -> None:
    """When repos.on_product_checkout_completed is None, a warning is logged
    and the event is still marked processed (no callback = no-op, not error)."""
    event_repo = InMemoryStripeEventRepository()
    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        # on_product_checkout_completed defaults to None
    )
    event = _payment_checkout_event()
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert event_repo._store[stripe_id].processed_at is not None
    assert event_repo._store[stripe_id].error is None


@pytest.mark.anyio
async def test_subscription_mode_still_routes_to_team_callback() -> None:
    """Subscription-mode checkouts with org metadata still go to the team
    callback — the new routing must not regress the existing path."""
    event_repo = InMemoryStripeEventRepository()
    team_calls: list[tuple[object, ...]] = []
    product_calls: list[tuple[object, ...]] = []

    async def _on_team(*args: object) -> None:
        team_calls.append(args)

    async def _on_product(*args: object) -> None:
        product_calls.append(args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_team_checkout_completed=_on_team,
        on_product_checkout_completed=_on_product,
    )
    event: dict[str, Any] = {
        "id": "evt_team_sub",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_team_sub",
                "mode": "subscription",
                "client_reference_id": "a1111111-0000-0000-0000-000000000000",
                "customer": "cus_team_sub_ref",
                "subscription": "sub_team_ref",
                "metadata": {"org_name": "Acme"},
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert len(team_calls) == 1
    assert product_calls == []
