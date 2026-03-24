"""GDPR compliance — right to erasure and right of access."""

from __future__ import annotations

import asyncio
from uuid import UUID

import stripe

from stripe_saas_core.exceptions import UserNotFoundError
from stripe_saas_core.repositories.customer import StripeCustomerRepository
from stripe_saas_core.repositories.subscription import SubscriptionRepository
from stripe_saas_core.repositories.user import UserRepository


async def delete_user_data(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
) -> None:
    """
    GDPR right to erasure — permanently remove all user data and Stripe resources.

    Sequence:
    1. Cancel any active Stripe subscription immediately (no grace period).
    2. Delete the Stripe Customer object (removes stored payment methods).
    3. Delete our StripeCustomer record.
    4. Soft-delete the user (sets deleted_at).
    """
    user, customer = await asyncio.gather(
        user_repo.get_by_id(user_id),
        customer_repo.get_by_user_id(user_id),
    )
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")

    if customer:
        active_sub = await subscription_repo.get_active_for_customer(customer.id)
        if active_sub:
            try:
                await asyncio.to_thread(stripe.Subscription.cancel, active_sub.stripe_id)
            except stripe.InvalidRequestError:
                pass  # already canceled in Stripe

        try:
            await asyncio.to_thread(stripe.Customer.delete, customer.stripe_id)
        except stripe.InvalidRequestError:
            pass  # already deleted in Stripe

        await customer_repo.delete(customer.id)

    await user_repo.delete(user_id)


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
    user, customer = await asyncio.gather(
        user_repo.get_by_id(user_id),
        customer_repo.get_by_user_id(user_id),
    )
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")

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
