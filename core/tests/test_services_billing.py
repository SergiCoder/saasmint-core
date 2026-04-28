"""Tests for services/billing.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from saasmint_core.exceptions import SubscriptionNotFoundError
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    create_product_checkout_session,
    create_team_stripe_customer,
    get_or_create_customer,
    resume_subscription,
)
from tests.conftest import (
    InMemoryStripeCustomerRepository,
    InMemorySubscriptionRepository,
    make_stripe_customer,
    make_subscription,
)

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


# ── cancel_subscription ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cancel_subscription_at_period_end() -> None:
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_cancel")
    await repo.save(sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=True,
            subscription_repo=repo,
        )

    mock_modify.assert_called_once_with("sub_cancel", cancel_at="min_period_end")


@pytest.mark.anyio
async def test_cancel_subscription_immediately() -> None:
    repo = InMemorySubscriptionRepository()
    customer_id = uuid4()
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_immed")
    await repo.save(sub)

    with patch("stripe.Subscription.cancel") as mock_cancel:
        await cancel_subscription(
            stripe_customer_id=customer_id,
            at_period_end=False,
            subscription_repo=repo,
        )

    mock_cancel.assert_called_once_with("sub_immed")


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
    sub = make_subscription(stripe_customer_id=customer_id, stripe_id="sub_resume")
    await repo.save(sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        await resume_subscription(
            stripe_customer_id=customer_id,
            subscription_repo=repo,
        )

    mock_modify.assert_called_once_with("sub_resume", cancel_at="")


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
