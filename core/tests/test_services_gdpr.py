"""Tests for services/gdpr.py — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
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

_SUPABASE_URL = "http://localhost:54321"
_SERVICE_ROLE_KEY = "test-service-role-key"


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
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
        )


@pytest.mark.anyio
async def test_request_deletion_no_subscription_deletes_immediately() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    with (
        patch(
            "saasmint_core.services.gdpr.delete_supabase_user", new_callable=AsyncMock
        ) as mock_supa,
        patch(
            "saasmint_core.services.gdpr.delete_supabase_avatar", new_callable=AsyncMock
        ) as mock_avatar,
    ):
        result = await request_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
        )

    assert result is None
    assert await user_repo.get_by_id(user.id) is None
    mock_supa.assert_called_once_with(
        supabase_url=_SUPABASE_URL,
        service_role_key=_SERVICE_ROLE_KEY,
        supabase_uid=user.supabase_uid,
    )
    mock_avatar.assert_called_once_with(
        supabase_url=_SUPABASE_URL,
        service_role_key=_SERVICE_ROLE_KEY,
        avatar_url=user.avatar_url,
    )


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
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
        )

    assert result == period_end
    mock_modify.assert_called_once_with("sub_sched", cancel_at_period_end=True)
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
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
        )

    assert result == period_end


# ── execute_account_deletion ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_execute_deletion_no_customer() -> None:
    user_repo = InMemoryUserRepository()
    customer_repo = InMemoryStripeCustomerRepository()
    subscription_repo = InMemorySubscriptionRepository()

    user = make_user()
    await user_repo.save(user)

    with (
        patch("saasmint_core.services.gdpr.delete_supabase_user", new_callable=AsyncMock),
        patch("saasmint_core.services.gdpr.delete_supabase_avatar", new_callable=AsyncMock),
    ):
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
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
    sub = make_subscription(stripe_customer_id=customer.id, stripe_id="sub_exec")
    await subscription_repo.save(sub)

    with (
        patch("stripe.Subscription.cancel") as mock_cancel,
        patch("stripe.Customer.delete") as mock_cust_del,
        patch("saasmint_core.services.gdpr.delete_supabase_user", new_callable=AsyncMock),
        patch("saasmint_core.services.gdpr.delete_supabase_avatar", new_callable=AsyncMock),
    ):
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
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
    sub = make_subscription(stripe_customer_id=customer.id, stripe_id="sub_gone")
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
        patch("saasmint_core.services.gdpr.delete_supabase_user", new_callable=AsyncMock),
        patch("saasmint_core.services.gdpr.delete_supabase_avatar", new_callable=AsyncMock),
    ):
        await execute_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            supabase_url=_SUPABASE_URL,
            service_role_key=_SERVICE_ROLE_KEY,
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
    sub = make_subscription(stripe_customer_id=customer.id, stripe_id="sub_reactivate")
    await subscription_repo.save(sub)

    with patch("stripe.Subscription.modify") as mock_modify:
        await cancel_account_deletion(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )

    mock_modify.assert_called_once_with("sub_reactivate", cancel_at_period_end=False)
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
