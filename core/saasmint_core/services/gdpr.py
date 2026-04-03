"""GDPR compliance — right to erasure and right of access."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

import stripe

from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.user import User
from saasmint_core.exceptions import UserNotFoundError
from saasmint_core.repositories.customer import StripeCustomerRepository
from saasmint_core.repositories.subscription import SubscriptionRepository
from saasmint_core.repositories.user import UserRepository
from saasmint_core.services.supabase_admin import delete_supabase_avatar, delete_supabase_user


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
    supabase_url: str,
    service_role_key: str,
) -> datetime | None:
    """
    Request GDPR right-to-erasure account deletion.

    Returns:
        None — account deleted immediately (no active subscription).
        datetime — scheduled deletion date (subscription period end).
    """
    _, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    active_sub = None
    if customer:
        active_sub = await subscription_repo.get_active_for_customer(customer.id)

    if active_sub:
        # Cancel renewal, schedule deletion for period end
        try:
            await asyncio.to_thread(
                stripe.Subscription.modify,
                active_sub.stripe_id,
                cancel_at_period_end=True,
            )
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":
                raise

        scheduled_at = active_sub.current_period_end
        await user_repo.schedule_deletion(user_id, scheduled_at)
        return scheduled_at

    # No active subscription — delete immediately
    await execute_account_deletion(
        user_id=user_id,
        user_repo=user_repo,
        customer_repo=customer_repo,
        subscription_repo=subscription_repo,
        supabase_url=supabase_url,
        service_role_key=service_role_key,
    )
    return None


async def execute_account_deletion(
    *,
    user_id: UUID,
    user_repo: UserRepository,
    customer_repo: StripeCustomerRepository,
    subscription_repo: SubscriptionRepository,
    supabase_url: str,
    service_role_key: str,
) -> None:
    """
    Execute GDPR right-to-erasure — permanently remove all user data.

    Called immediately when there is no active subscription, or by the
    scheduled-deletion Celery task after the subscription period ends.

    Sequence:
    1. Cancel any active Stripe subscription immediately.
    2. Delete the Stripe Customer object (removes stored payment methods).
    3. Delete our StripeCustomer record.
    4. Delete the Supabase Auth user.
    5. Hard-delete the user row (cascades to OrgMember, etc.).
    """
    user, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    if customer:
        active_sub = await subscription_repo.get_active_for_customer(customer.id)
        if active_sub:
            try:
                await asyncio.to_thread(stripe.Subscription.cancel, active_sub.stripe_id)
            except stripe.InvalidRequestError as exc:
                if exc.code != "resource_missing":
                    raise

        try:
            await asyncio.to_thread(stripe.Customer.delete, customer.stripe_id)
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":
                raise

        await customer_repo.delete(customer.id)

    await delete_supabase_avatar(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        avatar_url=user.avatar_url,
    )

    await delete_supabase_user(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
        supabase_uid=user.supabase_uid,
    )

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
    user, customer = await _load_user_and_customer(user_id, user_repo, customer_repo)

    if user.scheduled_deletion_at is None:
        return

    # Re-enable subscription renewal
    if customer:
        active_sub = await subscription_repo.get_active_for_customer(customer.id)
        if active_sub:
            try:
                await asyncio.to_thread(
                    stripe.Subscription.modify,
                    active_sub.stripe_id,
                    cancel_at_period_end=False,
                )
            except stripe.InvalidRequestError as exc:
                if exc.code != "resource_missing":
                    raise

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
