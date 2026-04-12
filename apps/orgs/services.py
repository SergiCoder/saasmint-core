"""Organisation lifecycle services — team checkout, member transitions, invitations."""

from __future__ import annotations

import asyncio
import logging
import re
from uuid import UUID

import stripe
from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.orgs.models import Invitation, InvitationStatus, Org, OrgMember, OrgRole
from apps.users.models import AccountType, User

logger = logging.getLogger(__name__)


def generate_unique_slug(name: str) -> str:
    """Generate a unique org slug from a name.

    Slugifies the name, ensures it matches [a-z0-9][a-z0-9-]*[a-z0-9] (min 2 chars),
    and appends a numeric suffix if the slug is already taken by an active org.
    """
    base = slugify(name)
    # Strip any characters not in [a-z0-9-]
    base = re.sub(r"[^a-z0-9-]", "", base)
    # Strip leading/trailing hyphens
    base = base.strip("-")
    # Ensure minimum length
    if len(base) < 2:
        base = "org"

    slug = base
    suffix = 2
    while Org.objects.filter(slug=slug, deleted_at__isnull=True).exists():
        slug = f"{base}-{suffix}"
        suffix += 1

    return slug


async def on_team_checkout_completed(
    user_id: UUID,
    org_name: str,
    stripe_subscription_id: str | None,
) -> None:
    """Create an org after a successful team plan checkout.

    Called from the checkout.session.completed webhook handler.
    Creates the Org, adds the user as owner + billing contact,
    updates account_type, and cancels any existing personal subscription.
    """
    user = await User.objects.aget(id=user_id)

    try:
        _org, _member = await sync_to_async(_create_org_with_owner)(user, org_name)
    except IntegrityError:
        logger.error(
            "Org creation failed during team checkout for user %s (name='%s')",
            user_id,
            org_name,
        )
        raise

    # Cancel any existing personal subscription with prorated refund
    await _cancel_personal_subscription(user_id)

    logger.info(
        "Team checkout completed: org '%s' (slug=%s) created for user %s",
        org_name,
        _org.slug,
        user_id,
    )


def _create_org_with_owner(user: User, org_name: str) -> tuple[Org, OrgMember]:
    """Atomically create an org and its owner membership."""
    with transaction.atomic():
        slug = generate_unique_slug(org_name)
        org = Org.objects.create(
            name=org_name,
            slug=slug,
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
