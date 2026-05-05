"""Tests for services/billing.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.exceptions import SubscriptionNotFoundError
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    create_product_checkout_session,
    create_team_stripe_customer,
    get_or_create_customer,
    release_pending_schedule_for_customer,
    resume_subscription,
)
from tests.conftest import (
    InMemoryStripeCustomerRepository,
    InMemorySubscriptionRepository,
    make_stripe_customer,
    make_subscription,
)


def _stripe_subscription_response(**fields: object) -> stripe.Subscription:
    """Build a Stripe-SDK Subscription object for mocking modify/cancel returns.

    Stripe's SDK methods echo back a ``stripe.Subscription`` (StripeObject
    subclass), not a plain dict. Tests that assert the cancel/resume mirror
    logic need attribute access (``stripe_sub.cancel_at``), so a bare dict
    will not work as a mock return value.
    """
    return stripe.Subscription.construct_from(fields, "sk_test")


# ── get_or_create_customer ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_or_create_customer_existing_by_user_id() -> None:
    user_id = uuid4()
    repo = InMemoryStripeCustomerRepository()
    existing = make_stripe_customer(user_id=user_id, stripe_id="cus_existing")
    await repo.save(existing)

    result = await get_or_create_customer(
        user_id=user_id,
        email="user@example.com",
        customer_repo=repo,
    )
    assert result.stripe_id == "cus_existing"


@pytest.mark.anyio
async def test_get_or_create_customer_existing_by_org_id() -> None:
    org_id = uuid4()
    repo = InMemoryStripeCustomerRepository()
    existing = make_stripe_customer(org_id=org_id, stripe_id="cus_org_existing")
    await repo.save(existing)

    result = await get_or_create_customer(
        org_id=org_id,
        email="org@example.com",
        customer_repo=repo,
    )
    assert result.stripe_id == "cus_org_existing"


@pytest.mark.anyio
async def test_get_or_create_customer_creates_new_for_user() -> None:
    user_id = uuid4()
    repo = InMemoryStripeCustomerRepository()

    mock_stripe_cust = MagicMock()
    mock_stripe_cust.id = "cus_new123"
    mock_stripe_cust.livemode = False

    with patch("stripe.Customer.create", return_value=mock_stripe_cust):
        result = await get_or_create_customer(
            user_id=user_id,
            email="new@example.com",
            name="New User",
            locale="en",
            customer_repo=repo,
        )

    assert result.stripe_id == "cus_new123"
    assert result.user_id == user_id
    assert result.org_id is None


@pytest.mark.anyio
async def test_get_or_create_customer_creates_new_for_org() -> None:
    org_id = uuid4()
    repo = InMemoryStripeCustomerRepository()

    mock_stripe_cust = MagicMock()
    mock_stripe_cust.id = "cus_org_new"
    mock_stripe_cust.livemode = True

    with patch("stripe.Customer.create", return_value=mock_stripe_cust):
        result = await get_or_create_customer(
            org_id=org_id,
            email="org@example.com",
            customer_repo=repo,
        )

    assert result.stripe_id == "cus_org_new"
    assert result.org_id == org_id
    assert result.user_id is None
    assert result.livemode is True


@pytest.mark.anyio
async def test_get_or_create_customer_neither_raises() -> None:
    repo = InMemoryStripeCustomerRepository()
    with pytest.raises(ValueError, match="user_id or org_id"):
        await get_or_create_customer(email="x@x.com", customer_repo=repo)


# ── create_team_stripe_customer ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_team_stripe_customer_returns_fresh_id() -> None:
    """Team checkout always mints a fresh Stripe customer (rule 3 / PR 5).
    The helper returns the new ``cus_…`` id and does not consult any repo —
    persistence is deferred to the ``checkout.session.completed`` webhook."""
    user_id = uuid4()
    mock_stripe_cust = MagicMock()
    mock_stripe_cust.id = "cus_team_fresh"

    with patch("stripe.Customer.create", return_value=mock_stripe_cust) as mock_create:
        result = await create_team_stripe_customer(
            user_id=user_id,
            email="upgrader@example.com",
            name="Upgrader",
            locale="en",
        )

    assert result == "cus_team_fresh"
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["email"] == "upgrader@example.com"
    assert call_kwargs["name"] == "Upgrader"
    assert call_kwargs["preferred_locales"] == ["en"]
    assert call_kwargs["metadata"] == {
        "user_id": str(user_id),
        "scope": "team_checkout",
    }


@pytest.mark.anyio
async def test_create_team_stripe_customer_each_call_mints_separate_customer() -> None:
    """Two team checkouts for the same user must produce two distinct
    Stripe customers — the helper has no de-duplication, by design. This
    guarantees the personal-vs-team customer split (rule 3) even when the
    user ran a prior team checkout that didn't complete."""
    user_id = uuid4()

    side_effects = [MagicMock(id="cus_team_first"), MagicMock(id="cus_team_second")]
    with patch("stripe.Customer.create", side_effect=side_effects) as mock_create:
        first = await create_team_stripe_customer(user_id=user_id, email="a@example.com")
        second = await create_team_stripe_customer(user_id=user_id, email="a@example.com")

    assert first == "cus_team_first"
    assert second == "cus_team_second"
    assert mock_create.call_count == 2


@pytest.mark.anyio
async def test_create_team_stripe_customer_defaults_locale_and_name() -> None:
    """Optional name / locale: defaults are ``None`` and ``"en"``. The
    Stripe SDK accepts ``name=None`` even though stubs declare ``str``
    (the existing ``# type: ignore[arg-type]`` documents this)."""
    user_id = uuid4()
    mock_stripe_cust = MagicMock()
    mock_stripe_cust.id = "cus_team_minimal"

    with patch("stripe.Customer.create", return_value=mock_stripe_cust) as mock_create:
        result = await create_team_stripe_customer(
            user_id=user_id,
            email="minimal@example.com",
        )

    assert result == "cus_team_minimal"
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["name"] is None
    assert call_kwargs["preferred_locales"] == ["en"]


# ── create_checkout_session ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_checkout_session_without_promo() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_test"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        url = await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            billing_currency="usd",
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    assert url == "https://checkout.stripe.com/pay/cs_test"
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["client_reference_id"] == "user_123"
    assert call_kwargs["allow_promotion_codes"] is True
    assert "discounts" not in call_kwargs
    assert "subscription_data" not in call_kwargs


@pytest.mark.anyio
async def test_create_checkout_session_with_trial_and_metadata() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_trial"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            billing_currency="usd",
            trial_period_days=14,
            metadata={"plan": "pro"},
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    call_kwargs = mock_create.call_args.kwargs
    sub_data = call_kwargs["subscription_data"]
    assert sub_data["trial_period_days"] == 14
    # Metadata is now at session level, not subscription_data level
    assert call_kwargs["metadata"] == {"plan": "pro"}


@pytest.mark.anyio
async def test_create_checkout_session_custom_quantity_and_locale() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_qty"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            billing_currency="usd",
            quantity=5,
            locale="es",
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["line_items"] == [{"price": "price_abc", "quantity": 5}]
    assert call_kwargs["locale"] == "es"


@pytest.mark.anyio
async def test_create_checkout_session_with_trial_only() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_trial_only"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            billing_currency="usd",
            trial_period_days=7,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    call_kwargs = mock_create.call_args.kwargs
    sub_data = call_kwargs["subscription_data"]
    assert sub_data["trial_period_days"] == 7
    assert "metadata" not in sub_data


@pytest.mark.anyio
async def test_create_checkout_session_with_metadata_only() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_meta"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            billing_currency="usd",
            metadata={"plan": "pro"},
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    call_kwargs = mock_create.call_args.kwargs
    # Metadata is now at session level, not in subscription_data
    assert call_kwargs["metadata"] == {"plan": "pro"}
    assert "subscription_data" not in call_kwargs


# ── create_product_checkout_session ───────────────────────────────────────────


@pytest.mark.anyio
async def test_create_product_checkout_session_uses_payment_mode() -> None:
    """One-time product purchases must be mode=payment (not subscription), and
    must not carry subscription_data/trial — that only applies to recurring."""
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_product"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        url = await create_product_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_boost_50",
            billing_currency="usd",
            success_url="https://example.com/ok",
            cancel_url="https://example.com/no",
        )

    assert url == "https://checkout.stripe.com/pay/cs_product"
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["mode"] == "payment"
    assert call_kwargs["line_items"] == [{"price": "price_boost_50", "quantity": 1}]
    assert call_kwargs["allow_promotion_codes"] is True
    assert call_kwargs["adaptive_pricing"] == {"enabled": True}
    assert "subscription_data" not in call_kwargs
    assert "metadata" not in call_kwargs


@pytest.mark.anyio
async def test_create_product_checkout_session_forwards_metadata() -> None:
    """metadata must be carried through — the webhook reads product_id/org_id
    from it to route the credit grant to the right owner."""
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_meta"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_product_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_boost_50",
            billing_currency="usd",
            success_url="https://example.com/ok",
            cancel_url="https://example.com/no",
            metadata={"product_id": "p_123", "org_id": "o_456"},
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["metadata"] == {"product_id": "p_123", "org_id": "o_456"}


@pytest.mark.anyio
async def test_create_product_checkout_session_forwards_locale() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_locale"

    with patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create:
        await create_product_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_boost_50",
            billing_currency="usd",
            locale="es",
            success_url="https://example.com/ok",
            cancel_url="https://example.com/no",
        )

    assert mock_create.call_args.kwargs["locale"] == "es"


# ── create_billing_portal_session ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_billing_portal_session() -> None:
    mock_session = MagicMock()
    mock_session.url = "https://billing.stripe.com/p/session_abc"

    with patch("stripe.billing_portal.Session.create", return_value=mock_session):
        url = await create_billing_portal_session(
            stripe_customer_id="cus_abc",
            locale="en",
            return_url="https://example.com/account",
        )

    assert url == "https://billing.stripe.com/p/session_abc"


@pytest.mark.anyio
async def test_create_billing_portal_session_with_flow_data() -> None:
    """When ``flow_data`` is provided it must be forwarded to Stripe's
    Session.create call — the branch that adds the key to ``params``."""
    mock_session = MagicMock()
    mock_session.url = "https://billing.stripe.com/p/session_flow"
    flow_data = {
        "type": "subscription_update_confirm",
        "subscription_update_confirm": {
            "subscription": "sub_xyz",
            "items": [{"id": "si_xyz", "price": "price_xyz", "quantity": 1}],
        },
    }

    with patch("stripe.billing_portal.Session.create", return_value=mock_session) as mock_create:
        url = await create_billing_portal_session(
            stripe_customer_id="cus_abc",
            locale="en",
            return_url="https://example.com/account",
            flow_data=flow_data,
        )

    assert url == "https://billing.stripe.com/p/session_flow"
    _, kwargs = mock_create.call_args
    assert kwargs["flow_data"] == flow_data


# ── cancel_subscription ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cancel_subscription_at_period_end() -> None:
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_cancel")
    await repo.save(sub)

    # Stripe's modify() echoes back the updated subscription. We mirror its
    # cancel_at into the local row synchronously so the frontend's PATCH-then-
    # GET path doesn't race the customer.subscription.updated webhook.
    stripe_response = _stripe_subscription_response(
        id="sub_cancel",
        status="active",
        cancel_at=1_780_000_000,
        canceled_at=None,
    )
    no_schedule = MagicMock()
    no_schedule.schedule = None
    with (
        patch("stripe.Subscription.retrieve", return_value=no_schedule),
        patch("stripe.Subscription.modify", return_value=stripe_response) as mock_modify,
    ):
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=True,
            subscription_repo=repo,
        )

    mock_modify.assert_called_once_with("sub_cancel", cancel_at="min_period_end")
    saved = await repo.get_active_for_customer(customer_id)
    assert saved is not None
    assert saved.cancel_at is not None
    assert int(saved.cancel_at.timestamp()) == 1_780_000_000


@pytest.mark.anyio
async def test_cancel_subscription_immediately() -> None:
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_immed")
    await repo.save(sub)

    # Immediate cancel (e.g. GDPR delete): Stripe transitions status to
    # canceled and sets canceled_at; mirror both onto the local row.
    stripe_response = _stripe_subscription_response(
        id="sub_immed",
        status="canceled",
        cancel_at=None,
        canceled_at=1_780_000_000,
    )
    no_schedule = MagicMock()
    no_schedule.schedule = None
    with (
        patch("stripe.Subscription.retrieve", return_value=no_schedule),
        patch("stripe.Subscription.cancel", return_value=stripe_response) as mock_cancel,
    ):
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=False,
            subscription_repo=repo,
        )

    mock_cancel.assert_called_once_with("sub_immed")
    # Status transitioned to canceled, so the row is no longer "active" — fetch
    # by stripe_id to confirm the mirrored fields landed.
    saved = await repo.get_by_stripe_id("sub_immed")
    assert saved is not None
    assert saved.status.value == "canceled"
    assert saved.canceled_at is not None
    assert int(saved.canceled_at.timestamp()) == 1_780_000_000


@pytest.mark.anyio
async def test_cancel_subscription_no_active_raises() -> None:
    repo = InMemorySubscriptionRepository()  # empty — no active sub

    with pytest.raises(SubscriptionNotFoundError):
        await cancel_subscription(
            stripe_customer_id=uuid4(),
            subscription_repo=repo,
        )


@pytest.mark.anyio
async def test_cancel_subscription_free_sub_raises() -> None:
    """A free-plan subscription has no stripe_id and cannot be canceled via Stripe."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    free_sub = make_subscription(stripe_customer_id=customer_id, stripe_id=None, user_id=uuid4())
    await repo.save(free_sub)

    with pytest.raises(SubscriptionNotFoundError):
        await cancel_subscription(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )


# ── resume_subscription ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_resume_subscription_clears_cancel_at() -> None:
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    # Sub was previously scheduled to cancel; resume should clear cancel_at
    # both upstream (Stripe) and locally (our mirror) before returning.
    sub = make_subscription(
        stripe_customer_id=customer_id,
        stripe_id="sub_resume",
        cancel_at=datetime.fromtimestamp(1_780_000_000, tz=UTC),
    )
    await repo.save(sub)

    stripe_response = _stripe_subscription_response(
        id="sub_resume",
        status="active",
        cancel_at=None,
        canceled_at=None,
    )
    with patch("stripe.Subscription.modify", return_value=stripe_response) as mock_modify:
        await resume_subscription(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )

    mock_modify.assert_called_once_with("sub_resume", cancel_at="")
    saved = await repo.get_active_for_customer(customer_id)
    assert saved is not None
    assert saved.cancel_at is None


@pytest.mark.anyio
async def test_resume_subscription_no_active_raises() -> None:
    repo = InMemorySubscriptionRepository()  # empty

    with pytest.raises(SubscriptionNotFoundError):
        await resume_subscription(
            stripe_customer_id=uuid4(),
            subscription_repo=repo,
        )


@pytest.mark.anyio
async def test_resume_subscription_free_sub_raises() -> None:
    """A free-plan subscription has no stripe_id and cannot be resumed via Stripe."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    free_sub = make_subscription(stripe_customer_id=customer_id, stripe_id=None, user_id=uuid4())
    await repo.save(free_sub)

    with pytest.raises(SubscriptionNotFoundError):
        await resume_subscription(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )


# ── schedule release ─────────────────────────────────────────────────────────


def _stripe_sub_with_schedule(schedule_id: str | None) -> MagicMock:
    """Mock a ``stripe.Subscription`` retrieve result for the schedule path.

    ``_release_pending_schedule`` reads ``sub.schedule`` (the schedule id
    pinning the sub, or ``None``) — that's the only attribute we need."""
    m = MagicMock()
    m.schedule = schedule_id
    return m


def _stripe_schedule(schedule_id: str, *, status: str) -> MagicMock:
    m = MagicMock()
    m.id = schedule_id
    m.status = status
    return m


@pytest.mark.anyio
async def test_cancel_subscription_releases_active_schedule_first() -> None:
    """If a SubscriptionSchedule is pinning the sub (deferred-downgrade
    awaiting period end), cancel must release it before calling
    ``Subscription.modify`` — otherwise Stripe rejects the cancel."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    # scheduled_plan_id must be non-None: cancel_subscription uses the local
    # mirror as a fast-path — when it's None no Stripe retrieve is issued
    # (common case: no pending schedule). Set it so the full release path runs.
    sub = make_subscription(
        stripe_customer_id=customer_id,
        stripe_id="sub_with_sched",
        scheduled_plan_id=uuid4(),
    )
    await repo.save(sub)

    stripe_response = _stripe_subscription_response(
        id="sub_with_sched", status="active", cancel_at=1_780_000_000, canceled_at=None
    )
    with (
        patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_sub_with_schedule("sub_sched_1"),
        ),
        patch(
            "stripe.SubscriptionSchedule.retrieve",
            return_value=_stripe_schedule("sub_sched_1", status="active"),
        ),
        patch("stripe.SubscriptionSchedule.release") as mock_release,
        patch("stripe.Subscription.modify", return_value=stripe_response),
    ):
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=True,
            subscription_repo=repo,
        )

    mock_release.assert_called_once_with("sub_sched_1")


@pytest.mark.anyio
async def test_cancel_subscription_skips_terminal_schedules() -> None:
    """Released/canceled/completed schedules are terminal — Stripe rejects
    further release calls on them. Cancel must skip those and proceed."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    # scheduled_plan_id must be non-None so the fast-path doesn't short-circuit
    # before we even try the retrieve. In production a terminal schedule would
    # still have a non-null mirror until the cleared webhook lands.
    sub = make_subscription(
        stripe_customer_id=customer_id,
        stripe_id="sub_done_sched",
        scheduled_plan_id=uuid4(),
    )
    await repo.save(sub)

    stripe_response = _stripe_subscription_response(
        id="sub_done_sched", status="active", cancel_at=1_780_000_000, canceled_at=None
    )
    with (
        patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_sub_with_schedule("sub_sched_done"),
        ),
        patch(
            "stripe.SubscriptionSchedule.retrieve",
            return_value=_stripe_schedule("sub_sched_done", status="released"),
        ),
        patch("stripe.SubscriptionSchedule.release") as mock_release,
        patch("stripe.Subscription.modify", return_value=stripe_response),
    ):
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=True,
            subscription_repo=repo,
        )

    mock_release.assert_not_called()


@pytest.mark.anyio
async def test_release_pending_schedule_for_customer_clears_local_mirror() -> None:
    """``release_pending_schedule_for_customer`` releases the Stripe schedule
    AND clears the local ``scheduled_plan_id`` / ``scheduled_change_at``
    fields up front so the immediate refetch reflects the cleared state
    without webhook lag."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(
        stripe_customer_id=customer_id,
        stripe_id="sub_pending",
        scheduled_plan_id=uuid4(),
        scheduled_change_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await repo.save(sub)

    with (
        patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_sub_with_schedule("sub_sched_pending"),
        ),
        patch(
            "stripe.SubscriptionSchedule.retrieve",
            return_value=_stripe_schedule("sub_sched_pending", status="active"),
        ),
        patch("stripe.SubscriptionSchedule.release") as mock_release,
    ):
        await release_pending_schedule_for_customer(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )

    mock_release.assert_called_once_with("sub_sched_pending")
    saved = await repo.get_active_for_customer(customer_id)
    assert saved is not None
    assert saved.scheduled_plan_id is None
    assert saved.scheduled_change_at is None


@pytest.mark.anyio
async def test_release_pending_schedule_for_customer_no_schedule_is_idempotent() -> None:
    """Calling release on a sub that has no schedule attached is a no-op
    (no Stripe call beyond the retrieve, no local change). Lets the API
    endpoint be safely called even when nothing is scheduled."""
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_clean")
    await repo.save(sub)

    with (
        patch(
            "stripe.Subscription.retrieve",
            return_value=_stripe_sub_with_schedule(None),
        ),
        patch("stripe.SubscriptionSchedule.release") as mock_release,
    ):
        await release_pending_schedule_for_customer(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )

    mock_release.assert_not_called()


@pytest.mark.anyio
async def test_release_pending_schedule_for_customer_no_active_sub_raises() -> None:
    repo = InMemorySubscriptionRepository()
    with pytest.raises(SubscriptionNotFoundError):
        await release_pending_schedule_for_customer(
            stripe_customer_id=uuid4(),
            subscription_repo=repo,
        )
