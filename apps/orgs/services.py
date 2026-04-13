"""Organization lifecycle services — team checkout, member management, invitations."""

from __future__ import annotations

import logging
import re
from uuid import UUID

import stripe
from asgiref.sync import async_to_sync
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
    stripe_customer_id: str,
    livemode: bool,
    stripe_subscription_id: str | None,
) -> None:
    """Create an org and its Stripe customer after a team plan checkout.

    Called from the checkout.session.completed webhook handler.
    The user already has account_type=ORG_MEMBER from registration.
    Creates the Org, adds the user as owner + billing contact, and
    links the Stripe customer to the org.
    """
    from asgiref.sync import sync_to_async

    from apps.billing.models import StripeCustomer

    user = await User.objects.aget(id=user_id)

    try:
        org, _member = await sync_to_async(_create_org_with_owner)(user, org_name)
    except IntegrityError:
        logger.error(
            "Org creation failed during team checkout for user %s (name='%s')",
            user_id,
            org_name,
        )
        raise

    await StripeCustomer.objects.acreate(
        stripe_id=stripe_customer_id,
        org=org,
        livemode=livemode,
    )

    logger.info(
        "Team checkout completed: org '%s' (slug=%s) created for user %s, Stripe customer %s",
        org_name,
        org.slug,
        user_id,
        stripe_customer_id,
    )


def _create_org_with_owner(user: User, org_name: str) -> tuple[Org, OrgMember]:
    """Atomically create an org and its owner membership.

    The user must already have account_type=ORG_MEMBER (set at registration).
    """
    if user.account_type != AccountType.ORG_MEMBER:
        raise ValueError(f"User {user.id} must have account_type=org_member to create an org")

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
    return org, member


async def cancel_pending_invitations_for_org(org_id: UUID) -> int:
    """Cancel all pending invitations for an org. Returns count cancelled."""
    count = await Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING).aupdate(
        status=InvitationStatus.CANCELLED
    )
    return count


def delete_org(org: Org) -> None:
    """Delete an org: cancel Stripe subs, hard-delete members and the org itself.

    Sequence: cancel Stripe sub → cancel invitations → collect member IDs →
    delete memberships → hard-delete all member user accounts → hard-delete org.
    """
    _cancel_team_subscription(org)
    async_to_sync(cancel_pending_invitations_for_org)(org.id)

    # Collect member user IDs before deleting anything
    member_user_ids = list(OrgMember.objects.filter(org=org).values_list("user_id", flat=True))

    OrgMember.objects.filter(org=org).delete()

    # Hard-delete all member user accounts (CASCADE handles related models)
    if member_user_ids:
        User.objects.filter(id__in=member_user_ids).delete()

    # Hard-delete the org itself
    org.delete()


def delete_orgs_created_by_user(user_id: UUID) -> None:
    """Delete all active orgs created by a user (used during account deletion)."""
    orgs = list(Org.objects.filter(created_by_id=user_id, deleted_at__isnull=True))
    for org in orgs:
        delete_org(org)


def decrement_subscription_seats(org_id: UUID) -> None:
    """Decrement the team subscription's seat count to match member count."""
    from saasmint_core.services.subscriptions import update_seat_count

    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org_id=org_id)
    except StripeCustomer.DoesNotExist:
        return

    try:
        sub = SubscriptionModel.objects.get(
            stripe_customer=customer,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
    except SubscriptionModel.DoesNotExist:
        return

    if sub.stripe_id is None:
        return

    new_quantity = OrgMember.objects.filter(org_id=org_id, org__deleted_at__isnull=True).count()

    if new_quantity < 1:
        return

    try:
        async_to_sync(update_seat_count)(
            stripe_subscription_id=sub.stripe_id,
            quantity=new_quantity,
        )
    except Exception:
        logger.exception(
            "Failed to update seat count to %d for sub %s",
            new_quantity,
            sub.stripe_id,
        )


def _cancel_team_subscription(org: Org) -> None:
    """Cancel the team subscription for an org via Stripe (immediate cancellation)."""
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org=org)
    except StripeCustomer.DoesNotExist:
        return

    subs = SubscriptionModel.objects.filter(
        stripe_customer=customer,
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        stripe_id__isnull=False,
    )
    for sub in subs:
        if sub.stripe_id is None:
            continue
        try:
            stripe.Subscription.cancel(sub.stripe_id)
        except stripe.StripeError:
            logger.exception(
                "Failed to cancel Stripe sub %s for org %s",
                sub.stripe_id,
                org.id,
            )
