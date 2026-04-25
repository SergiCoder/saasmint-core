"""Tests for services/gdpr.py — all branches covered."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.services.gdpr import (
    delete_account,
    export_user_data,
)
from tests.conftest import (
    InMemoryStripeCustomerRepository,
    InMemorySubscriptionRepository,
    InMemoryUserRepository,
    make_stripe_customer,
    make_subscription,
    make_user,
)

# ── delete_account ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_account_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await delete_account(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_delete_account_no_customer() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    await delete_account(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_account_customer_no_active_sub() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_no_sub")
    await customer_repo.save(customer)

    with patch("stripe.Customer.delete") as mock_cust_del:
        await delete_account(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cust_del.assert_called_once_with("cus_no_sub")
    assert await customer_repo.get_by_id(customer.id) is None
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_account_with_customer_and_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_exec")
    await customer_repo.save(customer)
    sub = make_subscription(stripe_customer_id=customer.id, user_id=user.id, stripe_id="sub_exec")
    await subscription_repo.save(sub)

    with (
        patch("stripe.Subscription.cancel") as mock_cancel,
        patch("stripe.Customer.delete") as mock_cust_del,
    ):
        await delete_account(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cancel.assert_called_once_with("sub_exec")
    mock_cust_del.assert_called_once_with("cus_exec")
    assert await customer_repo.get_by_id(customer.id) is None
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_account_stripe_already_gone() -> None:
    """resource_missing errors are swallowed for both subscription and customer."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_gone")
    await customer_repo.save(customer)
    sub = make_subscription(stripe_customer_id=customer.id, user_id=user.id, stripe_id="sub_gone")
    await subscription_repo.save(sub)

    with (
        patch(
            "stripe.Subscription.cancel",
            side_effect=stripe.InvalidRequestError(
                "already canceled", param="id", code="resource_missing"
            ),  # type: ignore[no-untyped-call]
        ),
        patch(
            "stripe.Customer.delete",
            side_effect=stripe.InvalidRequestError(
                "no such customer", param="id", code="resource_missing"
            ),  # type: ignore[no-untyped-call]
        ),
    ):
        await delete_account(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_account_with_no_subscription_skips_stripe_cancel() -> None:
    """A user without any Subscription row triggers no Stripe.cancel call."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    with patch("stripe.Subscription.cancel") as mock_cancel:
        await delete_account(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cancel.assert_not_called()
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_account_calls_pre_delete_hook() -> None:
    """pre_delete_hook is invoked with user_id before the user row is deleted."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    hook = AsyncMock()

    await delete_account(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
        pre_delete_hook=hook,
    )

    hook.assert_awaited_once_with(user.id)
    assert await user_repo.get_by_id(user.id) is None


# ── export_user_data ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_export_user_data_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await export_user_data(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_export_user_data_no_customer() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    result = await export_user_data(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert "user" in result
    assert "stripe_customer" not in result
    assert "subscription" not in result


@pytest.mark.anyio
async def test_export_user_data_with_customer_no_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_export")
    await customer_repo.save(customer)

    result = await export_user_data(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert result["stripe_customer"] == {
        "stripe_id": "cus_export",
        "livemode": False,
        "created_at": customer.created_at.isoformat(),
    }
    assert "subscription" not in result


@pytest.mark.anyio
async def test_export_user_data_with_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    sub = make_subscription(stripe_customer_id=customer.id, user_id=user.id)
    await subscription_repo.save(sub)

    result = await export_user_data(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert "subscription" in result
    sub_data = result["subscription"]
    assert isinstance(sub_data, dict)
