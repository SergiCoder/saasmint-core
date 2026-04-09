"""Tests for services/gdpr.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.services.gdpr import (
    cancel_account_deletion,
    execute_account_deletion,
    export_user_data,
    request_account_deletion,
)
from tests.conftest import (
    InMemoryStripeCustomerRepository,
    InMemorySubscriptionRepository,
    InMemoryUserRepository,
    make_stripe_customer,
    make_subscription,
    make_user,
)

# ── request_account_deletion ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_deletion_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await request_account_deletion(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_request_deletion_no_subscription_deletes_immediately() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    result = await request_account_deletion(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert result is None
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_request_deletion_with_active_subscription_schedules() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    period_end = datetime(2024, 2, 1, tzinfo=UTC)
    sub = make_subscription(
        stripe_customer_id=customer.id,
        user_id=user.id,
        stripe_id="sub_sched",
        current_period_end=period_end,
    )
    await subscription_repo.save(sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        result = await request_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    assert result == period_end
    mock_modify.assert_called_once_with("sub_sched", cancel_at="min_period_end")
    # User still exists with scheduled_deletion_at set
    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at == period_end


@pytest.mark.anyio
async def test_request_deletion_subscription_already_gone_in_stripe() -> None:
    """stripe.InvalidRequestError on modify is swallowed (resource_missing)."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    period_end = datetime(2024, 2, 1, tzinfo=UTC)
    sub = make_subscription(
        stripe_customer_id=customer.id,
        user_id=user.id,
        stripe_id="sub_gone",
        current_period_end=period_end,
    )
    await subscription_repo.save(sub)

    with patch(
        "stripe.Subscription.modify",
        side_effect=stripe.InvalidRequestError(
            "no such subscription", param="id", code="resource_missing"
        ),  # type: ignore[no-untyped-call]
    ):
        result = await request_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    assert result == period_end


# ── execute_account_deletion ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_execute_deletion_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await execute_account_deletion(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_execute_deletion_customer_no_active_sub() -> None:
    """Execute deletion with a customer but no active subscription."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id, stripe_id="cus_no_sub")
    await customer_repo.save(customer)

    with patch("stripe.Customer.delete") as mock_cust_del:
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cust_del.assert_called_once_with("cus_no_sub")
    assert await customer_repo.get_by_id(customer.id) is None
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_request_deletion_non_resource_missing_stripe_error_raises() -> None:
    """Non-resource_missing Stripe errors on modify should propagate."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    period_end = datetime(2024, 2, 1, tzinfo=UTC)
    sub = make_subscription(
        stripe_customer_id=customer.id,
        user_id=user.id,
        stripe_id="sub_err",
        current_period_end=period_end,
    )
    await subscription_repo.save(sub)

    with (
        patch(
            "stripe.Subscription.modify",
            side_effect=stripe.InvalidRequestError(
                "some other error", param="id", code="other_code"
            ),  # type: ignore[no-untyped-call]
        ),
        pytest.raises(stripe.InvalidRequestError),
    ):
        await request_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_execute_deletion_no_customer() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    await execute_account_deletion(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_execute_deletion_with_customer_and_subscription() -> None:
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
        await execute_account_deletion(
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
async def test_execute_deletion_stripe_already_gone() -> None:
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
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    assert await user_repo.get_by_id(user.id) is None


# ── cancel_account_deletion ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cancel_deletion_clears_schedule_and_reactivates_subscription() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user(scheduled_deletion_at=datetime(2024, 2, 1, tzinfo=UTC))
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    sub = make_subscription(
        stripe_customer_id=customer.id, user_id=user.id, stripe_id="sub_reactivate"
    )
    await subscription_repo.save(sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        await cancel_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_modify.assert_called_once_with("sub_reactivate", cancel_at="")
    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at is None


@pytest.mark.anyio
async def test_cancel_deletion_user_not_found_raises() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    with pytest.raises(UserNotFoundError):
        await cancel_account_deletion(
            user_id=uuid4(),
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_cancel_deletion_stripe_subscription_already_gone() -> None:
    """resource_missing error on subscription re-enable is swallowed."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user(scheduled_deletion_at=datetime(2024, 2, 1, tzinfo=UTC))
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    sub = make_subscription(
        stripe_customer_id=customer.id, user_id=user.id, stripe_id="sub_gone_cancel"
    )
    await subscription_repo.save(sub)

    with patch(
        "stripe.Subscription.modify",
        side_effect=stripe.InvalidRequestError(
            "no such subscription", param="id", code="resource_missing"
        ),  # type: ignore[no-untyped-call]
    ):
        await cancel_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at is None


@pytest.mark.anyio
async def test_cancel_deletion_stripe_non_resource_missing_raises() -> None:
    """Non-resource_missing stripe errors should propagate."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user(scheduled_deletion_at=datetime(2024, 2, 1, tzinfo=UTC))
    await user_repo.save(user)
    customer = make_stripe_customer(user_id=user.id)
    await customer_repo.save(customer)
    sub = make_subscription(
        stripe_customer_id=customer.id, user_id=user.id, stripe_id="sub_err_cancel"
    )
    await subscription_repo.save(sub)

    with (
        patch(
            "stripe.Subscription.modify",
            side_effect=stripe.InvalidRequestError(
                "some other error", param="id", code="other_error"
            ),  # type: ignore[no-untyped-call]
        ),
        pytest.raises(stripe.InvalidRequestError),
    ):
        await cancel_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )


@pytest.mark.anyio
async def test_cancel_deletion_no_customer_no_subscription() -> None:
    """Cancel deletion works even without a Stripe customer."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user(scheduled_deletion_at=datetime(2024, 2, 1, tzinfo=UTC))
    await user_repo.save(user)

    await cancel_account_deletion(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at is None


@pytest.mark.anyio
async def test_cancel_deletion_no_scheduled_deletion_is_noop() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    await cancel_account_deletion(
        user_id=user.id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )

    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at is None


# ── export_user_data ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_deletion_with_free_subscription_deletes_immediately() -> None:
    """A free subscription has no Stripe backing — treat as no sub and delete now."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    free_sub = make_subscription(
        user_id=user.id,
        stripe_id=None,
        stripe_customer_id=None,
    )
    await subscription_repo.save(free_sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        result = await request_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    # Free sub → immediate deletion path
    assert result is None
    mock_modify.assert_not_called()
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_execute_deletion_with_free_subscription_skips_stripe_cancel() -> None:
    """Free subs have no Stripe id; execute_account_deletion must not call Stripe.cancel."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)
    free_sub = make_subscription(user_id=user.id, stripe_id=None, stripe_customer_id=None)
    await subscription_repo.save(free_sub)

    with patch("stripe.Subscription.cancel") as mock_cancel:
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_cancel.assert_not_called()
    assert await user_repo.get_by_id(user.id) is None


@pytest.mark.anyio
async def test_cancel_deletion_with_free_subscription_skips_stripe_modify() -> None:
    """Cancelling deletion for a free-sub user must not call Stripe.modify."""
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user(scheduled_deletion_at=datetime(2024, 2, 1, tzinfo=UTC))
    await user_repo.save(user)
    free_sub = make_subscription(user_id=user.id, stripe_id=None, stripe_customer_id=None)
    await subscription_repo.save(free_sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        await cancel_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_modify.assert_not_called()
    stored_user = await user_repo.get_by_id(user.id)
    assert stored_user is not None
    assert stored_user.scheduled_deletion_at is None


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
