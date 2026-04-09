"""GDPR compliance — right to erasure and right of access."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import stripe

from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.user import User
from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.subscription import SubscriptionRepository
from saasmint_core.repositories.user import UserRepository


async def _stripe_request(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
    """Run a Stripe SDK call in a thread, ignoring 'resource_missing' errors."""
    try:
        await asyncio.to_thread(fn, *args, **kwargs)
    except stripe.InvalidRequestError as exc:
        if exc.code != "resource_missing":
            raise


async def _load_user_and_customer(
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
) -> tuple[User, StripeCustomer | None]:
    """Fetch user and customer in parallel, raising if user doesn't exist."""
    user, customer = await asyncio.gather(
        user_repo.get_by_id(user_id),
        customer_repo.get_by_user_id(user_id),
    )
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")
    return user, customer


async def request_account_deletion(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
) -> datetime | None:
    """
    Request GDPR right-to-erasure account deletion.

    Returns:
        None — account deleted immediately (no active subscription).
        datetime — scheduled deletion date (subscription period end).
    """
    await _load_user_and_customer(user_id, user_repo, customer_repo)

    active_sub = await subscription_repo.get_active_for_user(user_id)

    # Free-plan subscriptions have no Stripe record — treat as
    # "no subscription" so the account is deleted immediately.
    if active_sub and not active_sub.is_free:
        # Cancel renewal, schedule deletion for period end.
        # 2025-03-31.basil: `cancel_at_period_end=True` → `cancel_at="min_period_end"`.
        await _stripe_request(
            stripe.Subscription.modify,
            active_sub.stripe_id,
            cancel_at="min_period_end",
        )

        scheduled_at = active_sub.current_period_end
        await user_repo.schedule_deletion(user_id, scheduled_at)
        return scheduled_at

    # No active subscription — delete immediately
    await execute_account_deletion(
        user_id=user_id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
    )
    return None


async def execute_account_deletion(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Execute GDPR right-to-erasure — permanently remove all user data.

    Called immediately when there is no active subscription, or by the
    scheduled-deletion Celery task after the subscription period ends.

    Sequence:
    1. Cancel any active Stripe subscription immediately.
    2. Delete the Stripe Customer object (removes stored payment methods).
    3. Delete our StripeCustomer record.
    4. Hard-delete the user row (cascades to OrgMember, etc.).
    """
    _user, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    # Cancel any paid Stripe subscription
    active_sub = await subscription_repo.get_active_for_user(user_id)
    if active_sub and not active_sub.is_free:
        await _stripe_request(stripe.Subscription.cancel, active_sub.stripe_id)

    if customer:
        await _stripe_request(stripe.Customer.delete, customer.stripe_id)
        await customer_repo.delete(customer.id)

    await user_repo.hard_delete(user_id)


async def cancel_account_deletion(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    Cancel a previously scheduled account deletion.

    Re-enables subscription renewal and clears the scheduled deletion date.
    """
    user, _ = await _load_user_and_customer(user_id, user_repo, customer_repo)

    if user.scheduled_deletion_at is None:
        return

    # Re-enable subscription renewal (skip free subscriptions).
    # 2025-03-31.basil: clear a scheduled cancellation by passing `cancel_at=""`.
    active_sub = await subscription_repo.get_active_for_user(user_id)
    if active_sub and not active_sub.is_free:
        await _stripe_request(
            stripe.Subscription.modify,
            active_sub.stripe_id,
            cancel_at="",
        )

    await user_repo.cancel_scheduled_deletion(user_id)


async def export_user_data(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
) -> dict[str, object]:
    """
    GDPR right of access — return all stored user data as a JSON-serialisable dict.

    The response is suitable for sending directly to the user as a file download.
    """
    user, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    result: dict[str, object] = {"user": user.model_dump(mode="json")}
    if customer:
        result["stripe_customer"] = {
            "stripe_id": customer.stripe_id,
            "livemode": customer.livemode,
            "created_at": customer.created_at.isoformat(),
        }

        active_sub = await subscription_repo.get_active_for_customer(customer.id)
        if active_sub:
            result["subscription"] = active_sub.model_dump(mode="json")

    return result
