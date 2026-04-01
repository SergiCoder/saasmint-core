"""Tests for services/billing.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from saasmint_core.exceptions import InvalidPromoCodeError, SubscriptionNotFoundError
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    get_or_create_customer,
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
async def test_create_checkout_session_with_promo_code() -> None:
    mock_promo = MagicMock()
    mock_promo.id = "promo_id_abc"
    mock_coupon = MagicMock()
    mock_coupon.valid = True
    mock_promo.coupon = mock_coupon
    mock_list = MagicMock()
    mock_list.data = [mock_promo]

    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_promo"

    with (
        patch("stripe.PromotionCode.list", return_value=mock_list),
        patch("stripe.checkout.Session.create", return_value=mock_session) as mock_create,
    ):
        url = await create_checkout_session(
            stripe_customer_id="cus_abc",
            client_reference_id="user_123",
            price_id="price_abc",
            promo_code="SAVE20",
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )

    assert url == "https://checkout.stripe.com/pay/cs_promo"
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["allow_promotion_codes"] is False
    assert call_kwargs["discounts"] == [{"promotion_code": "promo_id_abc"}]


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
    assert sub_data["metadata"] == {"plan": "pro"}


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
    sub_data = call_kwargs["subscription_data"]
    assert sub_data["metadata"] == {"plan": "pro"}
    assert "trial_period_days" not in sub_data


@pytest.mark.anyio
async def test_create_checkout_session_invalid_promo_code_raises() -> None:
    mock_list = MagicMock()
    mock_list.data = []

    with patch("stripe.PromotionCode.list", return_value=mock_list):
        with pytest.raises(InvalidPromoCodeError):
            await create_checkout_session(
                stripe_customer_id="cus_abc",
                client_reference_id="user_123",
                price_id="price_abc",
                promo_code="BADCODE",
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
            )


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

    mock_modify.assert_called_once_with("sub_cancel", cancel_at_period_end=True)


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
