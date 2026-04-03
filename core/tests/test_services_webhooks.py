"""Tests for services/webhooks.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.domain.subscription import SubscriptionStatus
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
