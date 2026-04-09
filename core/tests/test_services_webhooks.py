"""Tests for services/webhooks.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.domain.subscription import Plan, PlanTier, SubscriptionStatus
from saasmint_core.exceptions import WebhookDataError, WebhookVerificationError
from saasmint_core.services.webhooks import WebhookRepos, handle_stripe_event
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
    discount: object = None,
    discounts: list[object] | None = None,
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
                "discount": discount,
                "discounts": discounts,
                "trial_end": trial_end,
                "canceled_at": canceled_at,
            }
        },
    }


# ── handle_stripe_event: top-level ───────────────────────────────────────────


@pytest.mark.anyio
async def test_invalid_signature_raises_webhook_verification_error() -> None:
    repos = _make_repos()
    with patch(
        "stripe.Webhook.construct_event",
        side_effect=stripe.error.SignatureVerificationError("bad sig", "t=1"),  # type: ignore[no-untyped-call]  # Stripe stub missing return type annotation on SignatureVerificationError constructor
    ):
        with pytest.raises(WebhookVerificationError):
            await handle_stripe_event(b"payload", "bad_sig", "secret", repos)


@pytest.mark.anyio
async def test_duplicate_event_is_no_op() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    # Pre-insert the event so it looks like a duplicate
    from saasmint_core.domain.stripe_event import StripeEvent

    existing = StripeEvent(
        id=uuid4(),
        stripe_id="evt_dup",
        type="invoice.payment_succeeded",
        livemode=False,
        payload={},
        created_at=datetime.now(UTC),
    )
    await event_repo.save(existing)

    mock_event = {
        "id": "evt_dup",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_123"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    # Event count unchanged (no second save)
    assert len(list(event_repo._store.values())) == 1


@pytest.mark.anyio
async def test_new_event_is_saved_and_processed() -> None:
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)

    mock_event = {
        "id": "evt_new",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_new"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    saved = event_repo._store["evt_new"]
    assert saved.processed_at is not None
    assert saved.error is None


@pytest.mark.anyio
async def test_dispatch_failure_marks_event_failed_and_reraises() -> None:
    event_repo = InMemoryStripeEventRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    repos = _make_repos(event_repo=event_repo, customer_repo=customer_repo)

    # customer.subscription.created will call _sync_subscription, which calls
    # customer_repo.get_by_stripe_id → returns None → no error raised in normal
    # flow (just logs warning). Use a different approach: raise from event_repo.mark_processed.
    # Instead, patch _dispatch directly to raise.
    mock_event = {
        "id": "evt_fail",
        "type": "customer.subscription.created",
        "livemode": False,
        "data": {"object": {}},
    }

    with (
        patch("stripe.Webhook.construct_event", return_value=mock_event),
        patch(
            "saasmint_core.services.webhooks._dispatch",
            side_effect=RuntimeError("dispatch boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="dispatch boom"):
            await handle_stripe_event(b"payload", "sig", "secret", repos)

    saved = event_repo._store["evt_fail"]
    assert saved.error == "dispatch boom"
    assert saved.processed_at is None


# ── _dispatch routing ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_dispatch_subscription_updated() -> None:
    """customer.subscription.updated also routes to _sync_subscription."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_upd")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_upd")
    plan_repo._prices[price.id] = price

    repos = _make_repos(customer_repo=customer_repo, plan_repo=plan_repo)
    event = _sub_event(
        "customer.subscription.updated", stripe_customer_id="cus_upd", price_id="price_upd"
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)


@pytest.mark.anyio
async def test_dispatch_invoice_payment_succeeded() -> None:
    repos = _make_repos()
    mock_event = {
        "id": "evt_inv_paid",
        "type": "invoice.payment_succeeded",
        "livemode": False,
        "data": {"object": {"id": "in_abc"}},
    }
    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)


@pytest.mark.anyio
async def test_dispatch_invoice_payment_failed() -> None:
    repos = _make_repos()
    mock_event = {
        "id": "evt_inv_fail",
        "type": "invoice.payment_failed",
        "livemode": False,
        "data": {"object": {"id": "in_fail"}},
    }
    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)


@pytest.mark.anyio
async def test_dispatch_unknown_event_type() -> None:
    repos = _make_repos()
    mock_event = {
        "id": "evt_unknown",
        "type": "some.unknown.event",
        "livemode": False,
        "data": {"object": {}},
    }
    with patch("stripe.Webhook.construct_event", return_value=mock_event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)


# ── _sync_subscription ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sync_subscription_customer_not_found_marks_failed() -> None:
    """Unknown customer → event marked as failed, error raised."""
    event_repo = InMemoryStripeEventRepository()
    repos = _make_repos(event_repo=event_repo)
    event = _sub_event("customer.subscription.created", stripe_customer_id="cus_unknown")

    with patch("stripe.Webhook.construct_event", return_value=event):
        with pytest.raises(WebhookDataError, match="Unknown customer"):
            await handle_stripe_event(b"payload", "sig", "secret", repos)

    saved = event_repo._store["evt_webhook"]
    assert saved.error is not None
    assert saved.processed_at is None


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

    with patch("stripe.Webhook.construct_event", return_value=event):
        with pytest.raises(WebhookDataError, match="Unknown price"):
            await handle_stripe_event(b"payload", "sig", "secret", repos)

    saved = event_repo._store["evt_webhook"]
    assert saved.error is not None
    assert saved.processed_at is None


@pytest.mark.anyio
async def test_sync_subscription_creates_new() -> None:
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
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_new_sub",
        price_id="price_new_sub",
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    subs = list(subscription_repo._store.values())
    assert len(subs) == 1
    assert subs[0].stripe_id == "sub_webhook"
    assert subs[0].status == SubscriptionStatus.ACTIVE


@pytest.mark.anyio
async def test_sync_subscription_updates_existing() -> None:
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_upd_sub")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_upd_sub")
    plan_repo._prices[price.id] = price

    # Pre-existing subscription with same stripe_id
    existing_sub = make_subscription(
        stripe_id="sub_webhook",
        stripe_customer_id=customer.id,
    )
    await subscription_repo.save(existing_sub)

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.updated",
        stripe_customer_id="cus_upd_sub",
        price_id="price_upd_sub",
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    updated = subscription_repo._store[existing_sub.id]
    assert updated.id == existing_sub.id  # same ID preserved


@pytest.mark.anyio
async def test_sync_subscription_with_full_discount_and_trial() -> None:
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_discount")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_discount")
    plan_repo._prices[price.id] = price

    discount = {
        "promotion_code": "promo_abc",
        "coupon": {"percent_off": 20},
        "end": NOW_TS + 86400,
    }

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_discount",
        price_id="price_discount",
        discount=discount,
        trial_end=NOW_TS + 604800,
        canceled_at=None,
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.promotion_code_id == "promo_abc"
    assert sub.discount_percent == 20.0
    assert sub.discount_end_at is not None
    assert sub.trial_ends_at is not None


@pytest.mark.anyio
async def test_sync_subscription_with_discount_no_promo_no_percent() -> None:
    """Discount block present but no promotion_code and no percent_off."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_disc2")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_disc2")
    plan_repo._prices[price.id] = price

    discount = {
        "promotion_code": None,
        "coupon": {"percent_off": None},
        "end": None,
    }

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_disc2",
        price_id="price_disc2",
        discount=discount,
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.promotion_code_id is None
    assert sub.discount_percent is None
    assert sub.discount_end_at is None


@pytest.mark.anyio
async def test_sync_subscription_with_discount_coupon_none() -> None:
    """Discount present but coupon key is None — exercises the `or {}` fallback."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_disc3")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_disc3")
    plan_repo._prices[price.id] = price

    discount = {
        "promotion_code": "promo_xyz",
        "coupon": None,
        "end": NOW_TS + 86400,
    }

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_disc3",
        price_id="price_disc3",
        discount=discount,
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.promotion_code_id == "promo_xyz"
    assert sub.discount_percent is None
    assert sub.discount_end_at is not None


@pytest.mark.anyio
async def test_sync_subscription_with_basil_discounts_array() -> None:
    """Stripe API 2026-03-25.dahlia: ``discounts[]`` array replaces singular ``discount``."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_basil")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_basil")
    plan_repo._prices[price.id] = price

    discounts = [
        {
            "promotion_code": "promo_basil",
            "coupon": {"percent_off": 15},
            "end": NOW_TS + 86400,
        }
    ]

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_basil",
        price_id="price_basil",
        discounts=discounts,
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.promotion_code_id == "promo_basil"
    assert sub.discount_percent == 15.0
    assert sub.discount_end_at is not None


@pytest.mark.anyio
async def test_sync_subscription_quantity_none_defaults_to_one() -> None:
    """Missing quantity in subscription item defaults to 1."""
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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.quantity == 1


@pytest.mark.anyio
async def test_sync_subscription_with_explicit_quantity() -> None:
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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.quantity == 5


@pytest.mark.anyio
async def test_sync_subscription_with_canceled_at() -> None:
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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.canceled_at is not None


# ── _on_subscription_deleted ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_subscription_deleted_unknown_sub_logs_warning() -> None:
    repos = _make_repos()
    event = {
        "id": "evt_del_unk",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_unknown"}},
    }
    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)


@pytest.mark.anyio
async def test_subscription_deleted_marks_canceled() -> None:
    subscription_repo = InMemorySubscriptionRepository()
    sub = make_subscription(stripe_id="sub_to_delete")
    await subscription_repo.save(sub)

    repos = _make_repos(subscription_repo=subscription_repo)
    event = {
        "id": "evt_del_known",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_to_delete"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

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
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()

    # Pre-existing free subscription (no Stripe backing)
    free_plan = _seed_free_plan(plan_repo)
    free_sub = make_subscription(
        stripe_id=None,
        stripe_customer_id=None,
        user_id=user_id,
        plan_id=free_plan.id,
    )
    await subscription_repo.save(free_sub)

    # Customer now exists (created during checkout flow)
    customer = make_stripe_customer(user_id=user_id, stripe_id="cus_upgrade")
    await customer_repo.save(customer)

    # New paid plan + price
    paid_plan = make_plan(name="Personal Pro")
    plan_repo._plans[paid_plan.id] = paid_plan
    paid_price = make_plan_price(plan_id=paid_plan.id, stripe_price_id="price_paid", amount=1900)
    plan_repo._prices[paid_price.id] = paid_price

    repos = _make_repos(
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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

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

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    subs = list(subscription_repo._store.values())
    assert len(subs) == 1
    assert subs[0].stripe_id == "sub_retry"
    assert free_sub.id not in subscription_repo._store


@pytest.mark.anyio
async def test_paid_cancellation_creates_fresh_free_subscription() -> None:
    """When a personal paid sub is canceled, the user is moved back to free."""
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user_id = uuid4()
    free_plan = _seed_free_plan(plan_repo)

    paid_sub = make_subscription(
        stripe_id="sub_paid_cancel",
        user_id=user_id,
    )
    await subscription_repo.save(paid_sub)

    repos = _make_repos(plan_repo=plan_repo, subscription_repo=subscription_repo)
    event = {
        "id": "evt_cancel_fallback",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_paid_cancel"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

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
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()
    _seed_free_plan(plan_repo)

    org_paid_sub = make_subscription(stripe_id="sub_org_cancel", user_id=None)
    await subscription_repo.save(org_paid_sub)

    repos = _make_repos(plan_repo=plan_repo, subscription_repo=subscription_repo)
    event = {
        "id": "evt_org_cancel",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_org_cancel"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    assert subscription_repo._store[org_paid_sub.id].status == SubscriptionStatus.CANCELED
    # No new free subscription created
    assert len(subscription_repo._store) == 1


@pytest.mark.anyio
async def test_cancellation_logs_when_no_free_plan_configured() -> None:
    """Without a configured free plan, cancellation just marks canceled and warns."""
    plan_repo = InMemoryPlanRepository()  # no free plan seeded
    subscription_repo = InMemorySubscriptionRepository()

    paid_sub = make_subscription(stripe_id="sub_no_free", user_id=uuid4())
    await subscription_repo.save(paid_sub)

    repos = _make_repos(plan_repo=plan_repo, subscription_repo=subscription_repo)
    event = {
        "id": "evt_no_free",
        "type": "customer.subscription.deleted",
        "livemode": False,
        "data": {"object": {"id": "sub_no_free"}},
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    assert subscription_repo._store[paid_sub.id].status == SubscriptionStatus.CANCELED
    assert len(subscription_repo._store) == 1  # no fallback created


# ── Basil API: period fields on items + discount edge cases ─────────────────


@pytest.mark.anyio
async def test_sync_subscription_reads_period_from_items_first() -> None:
    """Stripe API 2024-06+ moved current_period_start/end to subscription items."""
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
                "discount": None,
                "discounts": None,
                "trial_end": None,
                "canceled_at": None,
            }
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert int(sub.current_period_start.timestamp()) == item_start_ts
    assert int(sub.current_period_end.timestamp()) == item_end_ts


@pytest.mark.anyio
async def test_sync_subscription_missing_period_raises_webhook_data_error() -> None:
    """Missing current_period_start/end raises WebhookDataError."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    event_repo = InMemoryStripeEventRepository()

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
                "discount": None,
                "discounts": None,
                "trial_end": None,
                "canceled_at": None,
            }
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=event):
        with pytest.raises(WebhookDataError, match="missing current_period"):
            await handle_stripe_event(b"payload", "sig", "secret", repos)


@pytest.mark.anyio
async def test_sync_subscription_discounts_array_with_string_ids_ignored() -> None:
    """When discounts[] contains string IDs (unexpanded), discount is treated as None."""
    customer_repo = InMemoryStripeCustomerRepository()
    plan_repo = InMemoryPlanRepository()
    subscription_repo = InMemorySubscriptionRepository()

    customer = make_stripe_customer(user_id=uuid4(), stripe_id="cus_str_disc")
    await customer_repo.save(customer)
    plan = make_plan()
    plan_repo._plans[plan.id] = plan
    price = make_plan_price(plan_id=plan.id, stripe_price_id="price_str_disc")
    plan_repo._prices[price.id] = price

    repos = _make_repos(
        customer_repo=customer_repo,
        plan_repo=plan_repo,
        subscription_repo=subscription_repo,
    )
    event = _sub_event(
        "customer.subscription.created",
        stripe_customer_id="cus_str_disc",
        price_id="price_str_disc",
        discounts=["di_string_only"],
    )

    with patch("stripe.Webhook.construct_event", return_value=event):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    sub = next(iter(subscription_repo._store.values()))
    assert sub.promotion_code_id is None
    assert sub.discount_percent is None


@pytest.mark.anyio
async def test_handle_stripe_event_converts_stripe_object_via_to_dict() -> None:
    """When construct_event returns a StripeObject-like object, to_dict() is called."""
    repos = _make_repos()

    class FakeStripeObject:
        def to_dict(self) -> dict[str, object]:
            return {
                "id": "evt_stripe_obj",
                "type": "invoice.payment_succeeded",
                "livemode": False,
                "data": {"object": {"id": "in_fake"}},
            }

    with patch("stripe.Webhook.construct_event", return_value=FakeStripeObject()):
        await handle_stripe_event(b"payload", "sig", "secret", repos)

    # Event was saved — verify it was processed successfully
    saved = repos.events._store["evt_stripe_obj"]  # type: ignore[attr-defined]
    assert saved.processed_at is not None
    assert saved.error is None
