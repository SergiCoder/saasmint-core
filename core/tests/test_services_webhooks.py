"""Tests for services/webhooks.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import stripe

from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import SubscriptionStatus
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


# ── cancellation has no fallback (Subscription is a pure Stripe mirror) ─────


@pytest.mark.anyio
async def test_paid_cancellation_marks_canceled_without_fallback() -> None:
    """A canceled personal paid sub stays as the only row — no free fallback."""
    event_repo = InMemoryStripeEventRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()
    paid_sub = make_subscription(stripe_id="sub_paid_cancel", user_id=user_id)
    await subscription_repo.save(paid_sub)

    repos = _make_repos(
        event_repo=event_repo, plan_repo=plan_repo, subscription_repo=subscription_repo
    )
    event = {
        "id": "evt_cancel_no_fallback",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_paid_cancel"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    canceled = subscription_repo._store[paid_sub.id]
    assert canceled.status == SubscriptionStatus.CANCELED
    assert canceled.canceled_at is not None
    # Subscription is a pure Stripe mirror — no free row is created.
    assert len(subscription_repo._store) == 1


@pytest.mark.anyio
async def test_org_cancellation_marks_canceled_only() -> None:
    """Org subs (user_id=None) are also flipped to CANCELED with no extra row."""
    event_repo = InMemoryStripeEventRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

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
    assert len(subscription_repo._store) == 1


@pytest.mark.anyio
async def test_org_cancellation_invokes_delete_callback() -> None:
    """When an org-scoped sub (user_id=None, customer.org_id set) is canceled,
    the on_org_subscription_canceled callback fires with the org id so the
    Django side can hard-delete the org. Mirrors the wiring of
    apps.orgs.services.delete_org_on_subscription_cancel."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    org_id = uuid4()
    customer = make_stripe_customer(org_id=org_id, stripe_id="cus_org_cb")
    await customer_repo.save(customer)
    org_sub = make_subscription(
        stripe_id="sub_org_cb", user_id=None, stripe_customer_id=customer.id
    )
    await subscription_repo.save(org_sub)

    received: list[UUID] = []

    async def _on_org_canceled(arg_org_id: UUID) -> None:
        received.append(arg_org_id)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=subscription_repo,
        customers=customer_repo,
        plans=InMemoryPlanRepository(),
        on_org_subscription_canceled=_on_org_canceled,
    )
    event = {
        "id": "evt_org_cb",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_org_cb"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert received == [org_id]
    assert subscription_repo._store[org_sub.id].status == SubscriptionStatus.CANCELED


@pytest.mark.anyio
async def test_personal_team_customer_does_not_invoke_org_delete_callback() -> None:
    """When the StripeCustomer has user_id but no org_id (e.g. team checkout
    that hasn't yet rebound to an org, or a personal sub), the org-delete
    callback must not fire."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_no_org")
    await customer_repo.save(customer)
    org_sub = make_subscription(
        stripe_id="sub_no_org", user_id=None, stripe_customer_id=customer.id
    )
    await subscription_repo.save(org_sub)

    received: list[UUID] = []

    async def _on_org_canceled(arg_org_id: UUID) -> None:
        received.append(arg_org_id)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=subscription_repo,
        customers=customer_repo,
        plans=InMemoryPlanRepository(),
        on_org_subscription_canceled=_on_org_canceled,
    )
    event = {
        "id": "evt_no_org_cb",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_no_org"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert received == []


@pytest.mark.anyio
async def test_org_cancellation_without_callback_logs_and_succeeds() -> None:
    """If on_org_subscription_canceled is not registered, the event is still
    processed (sub flipped to CANCELED) and the missing-callback warning path
    runs without raising."""
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    org_id = uuid4()
    customer = make_stripe_customer(org_id=org_id, stripe_id="cus_no_cb")
    await customer_repo.save(customer)
    org_sub = make_subscription(stripe_id="sub_no_cb", user_id=None, stripe_customer_id=customer.id)
    await subscription_repo.save(org_sub)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=subscription_repo,
        customers=customer_repo,
        plans=InMemoryPlanRepository(),
        # on_org_subscription_canceled intentionally omitted
    )
    event = {
        "id": "evt_no_cb",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_no_cb"}},
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert subscription_repo._store[org_sub.id].status == SubscriptionStatus.CANCELED
    assert event_repo._store[stripe_id].processed_at is not None
    assert event_repo._store[stripe_id].error is None


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


@pytest.mark.anyio
async def test_team_callback_passes_keep_personal_subscription_true() -> None:
    """PR 5: ``keep_personal_subscription=true`` in session metadata must
    decode back to a boolean ``True`` for the callback. Stripe metadata is
    string-typed, so the wire form is the literal ``"true"``."""
    event_repo = InMemoryStripeEventRepository()
    team_calls: list[tuple[object, ...]] = []

    async def _on_team(*args: object) -> None:
        team_calls.append(args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_team_checkout_completed=_on_team,
    )
    event: dict[str, Any] = {
        "id": "evt_team_keep",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_team_keep",
                "mode": "subscription",
                "client_reference_id": "a1111111-0000-0000-0000-000000000000",
                "customer": "cus_keep",
                "subscription": "sub_keep",
                "metadata": {"org_name": "KeepOrg", "keep_personal_subscription": "true"},
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert len(team_calls) == 1
    # Callback signature: (user_id, org_name, customer, livemode, sub_id, keep_personal)
    assert team_calls[0][5] is True


@pytest.mark.anyio
async def test_team_callback_defaults_keep_personal_subscription_to_false() -> None:
    """Missing or non-``"true"`` value for ``keep_personal_subscription``
    decodes to ``False`` — matches PR 5's default behavior (auto-cancel
    personal at period end)."""
    event_repo = InMemoryStripeEventRepository()
    team_calls: list[tuple[object, ...]] = []

    async def _on_team(*args: object) -> None:
        team_calls.append(args)

    repos = WebhookRepos(
        events=event_repo,
        subscriptions=InMemorySubscriptionRepository(),
        customers=InMemoryStripeCustomerRepository(),
        plans=InMemoryPlanRepository(),
        on_team_checkout_completed=_on_team,
    )
    event: dict[str, Any] = {
        "id": "evt_team_default",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_team_default",
                "mode": "subscription",
                "client_reference_id": "a1111111-0000-0000-0000-000000000000",
                "customer": "cus_default",
                "subscription": "sub_default",
                "metadata": {"org_name": "DefaultOrg"},  # no keep_personal_subscription
            }
        },
    }
    stripe_id = await _persist(event_repo, event)

    await process_stored_event(event, stripe_id, repos)

    assert len(team_calls) == 1
    assert team_calls[0][5] is False
