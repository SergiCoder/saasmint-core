"""Tests for services/gdpr.py — all branches covered."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.services.gdpr import delete_user_data, export_user_data
from tests.conftest import (
    InMemoryStripeCustomerRepository,
    InMemorySubscriptionRepository,
    InMemoryUserRepository,
    make_stripe_customer,
    make_subscription,
    make_user,
)

# ── delete_user_data ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_delete_user_data_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await delete_user_data(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_delete_user_data_no_customer_deletes_user() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    await delete_user_data(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_user_data_with_customer_no_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_del")
    await customer_repo.save(customer)

    with patch("stripe.Customer.delete") as mock_del:
        await delete_user_data(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_del.assert_called_once_with("cus_del")
    assert await customer_repo.get_by_id(customer.id) is None
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_delete_user_data_with_active_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_sub")
    await customer_repo.save(customer)
    sub = make_subscription(stripe_customer_id=customer.id, stripe_id="sub_active")
    await subscription_repo.save(sub)

    with (
        patch("stripe.Subscription.cancel") as mock_cancel,
        patch("stripe.Customer.delete"),
    ):
        await delete_user_data(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cancel.assert_called_once_with("sub_active")


@pytest.mark.anyio
async def test_delete_user_data_subscription_already_canceled_in_stripe() -> None:
    """stripe.InvalidRequestError on cancel is swallowed (already canceled)."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_sub2")
    await customer_repo.save(customer)
    sub = make_subscription(stripe_customer_id=customer.id, stripe_id="sub_gone")
    await subscription_repo.save(sub)

    with (
        patch(
            "stripe.Subscription.cancel",
            side_effect=stripe.InvalidRequestError(
                "already canceled", param="id", code="resource_missing"
            ),  # type: ignore[no-untyped-call]  # Stripe stub missing return type annotation on InvalidRequestError constructor
        ),
        patch("stripe.Customer.delete"),
    ):
        # Should not raise
        await delete_user_data(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_delete_user_data_customer_already_deleted_in_stripe() -> None:
    """stripe.InvalidRequestError on customer delete is swallowed."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_gone")
    await customer_repo.save(customer)

    with patch(
        "stripe.Customer.delete",
        side_effect=stripe.InvalidRequestError(
            "no such customer", param="id", code="resource_missing"
        ),  # type: ignore[no-untyped-call]
    ):
        # Should not raise
        await delete_user_data(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


# ── export_user_data ──────────────────────────────────────────────────────────


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
    sub = make_subscription(stripe_customer_id=customer.id)
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
