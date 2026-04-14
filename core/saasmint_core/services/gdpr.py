"""GDPR compliance — right to erasure and right of access."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import stripe

from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.user import User
from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.subscription import SubscriptionRepository
from saasmint_core.repositories.user import UserRepository

# Called before the user row is hard-deleted, to clean up
# resources that hold a PROTECT FK to the user (e.g. orgs).
PreDeleteHook = Callable[[UUID], Awaitable[None]]


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


async def delete_account(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
    pre_delete_hook: PreDeleteHook | None = None,
) -> None:
    """
    GDPR right-to-erasure — permanently remove all user data.

    Sequence:
    1. Cancel any active Stripe subscription immediately.
    2. Delete the Stripe Customer object (removes stored payment methods).
    3. Delete our StripeCustomer record.
    4. Run pre_delete_hook (e.g. delete orgs with PROTECT FK).
    5. Hard-delete the user row (cascades to OrgMember, etc.).
    """
    _user, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    # Cancel any paid Stripe subscription
    active_sub = await subscription_repo.get_active_for_user(user_id)
    if active_sub and not active_sub.is_free:
        await _stripe_request(stripe.Subscription.cancel, active_sub.stripe_id)

    if customer:
        await _stripe_request(stripe.Customer.delete, customer.stripe_id)
        await customer_repo.delete(customer.id)

    if pre_delete_hook:
        await pre_delete_hook(user_id)

    await user_repo.hard_delete(user_id)


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
