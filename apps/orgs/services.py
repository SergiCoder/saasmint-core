"""Organisation lifecycle services — team checkout, member transitions, invitations."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import stripe
from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction

from apps.orgs.models import Invitation, InvitationStatus, Org, OrgMember, OrgRole
from apps.users.models import AccountType, User

logger = logging.getLogger(__name__)


async def on_team_checkout_completed(
    user_id: UUID,
    org_name: str,
    org_slug: str,
    stripe_subscription_id: str | None,
) -> None:
    """Create an org after a successful team plan checkout.

    Called from the checkout.session.completed webhook handler.
    Creates the Org, adds the user as owner + billing contact,
    updates account_type, and cancels any existing personal subscription.
    """
    user = await User.objects.aget(id=user_id)

    try:
        _org, _member = await sync_to_async(_create_org_with_owner)(user, org_name, org_slug)
    except IntegrityError:
        logger.error(
            "Org slug '%s' already taken during team checkout for user %s",
            org_slug,
            user_id,
        )
        raise

    # Cancel any existing personal subscription with prorated refund
    await _cancel_personal_subscription(user_id)

    logger.info(
        "Team checkout completed: org '%s' (slug=%s) created for user %s",
        org_name,
        org_slug,
        user_id,
    )


def _create_org_with_owner(user: User, org_name: str, org_slug: str) -> tuple[Org, OrgMember]:
    """Atomically create an org and its owner membership."""
    with transaction.atomic():
        org = Org.objects.create(
            name=org_name,
            slug=org_slug,
            created_by=user,
        )
        member = OrgMember.objects.create(
            org=org,
            user=user,
            role=OrgRole.OWNER,
            is_billing=True,
        )
        user.account_type = AccountType.ORG_MEMBER
        user.save(update_fields=["account_type"])
    return org, member


async def _cancel_personal_subscription(user_id: UUID) -> None:
    """Cancel a user's personal paid subscription with prorated refund, if any."""
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        sub = await SubscriptionModel.objects.aget(
            user_id=user_id,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
    except SubscriptionModel.DoesNotExist:
        return
    except SubscriptionModel.MultipleObjectsReturned:
        sub = await SubscriptionModel.objects.filter(
            user_id=user_id,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        ).alatest("created_at")

    # Cancel immediately on Stripe (prorated refund happens automatically
    # when proration_behavior is set). The DB record is synced via the
    # customer.subscription.deleted webhook.
    stripe_id: str = sub.stripe_id  # type: ignore[assignment]  # checked above via stripe_id__isnull=False
    await asyncio.to_thread(
        stripe.Subscription.cancel,
        stripe_id,
        prorate=True,
    )

    # Also delete the free subscription if any
    await SubscriptionModel.objects.filter(user_id=user_id, stripe_id__isnull=True).adelete()

    logger.info(
        "Cancelled personal subscription %s for user %s (team join)",
        sub.stripe_id,
        user_id,
    )


async def revert_to_personal(user: User) -> None:
    """Revert a user's account_type to personal and assign a free plan.

    Used when a user leaves/is removed from an org.
    """
    from apps.billing.services import assign_free_plan

    user.account_type = AccountType.PERSONAL
    await user.asave(update_fields=["account_type"])
    await sync_to_async(assign_free_plan)(user)


async def cancel_pending_invitations_for_org(org_id: UUID) -> int:
    """Cancel all pending invitations for an org. Returns count cancelled."""
    count = await Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING).aupdate(
        status=InvitationStatus.CANCELLED
    )
    return count
