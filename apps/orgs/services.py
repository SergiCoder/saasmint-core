"""Organization lifecycle services — team checkout, member management, invitations."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from apps.billing.models import Subscription as SubscriptionModel

import stripe
from asgiref.sync import async_to_sync, sync_to_async
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

    # Pull every existing variant in one query (`base`, `base-2`, `base-3`, ...)
    # and pick the lowest free suffix. Avoids O(N) `exists()` calls for hot slugs.
    existing = set(
        Org.objects.filter(
            slug__regex=rf"^{re.escape(base)}(-\d+)?$",
            deleted_at__isnull=True,
        ).values_list("slug", flat=True)
    )
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


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
    user = await User.objects.aget(id=user_id)

    try:
        org, _member = await sync_to_async(_create_org_with_owner)(
            user,
            org_name,
            stripe_customer_id=stripe_customer_id,
            livemode=livemode,
        )
    except IntegrityError:
        logger.error(
            "Org creation failed during team checkout for user %s (name='%s')",
            user_id,
            org_name,
        )
        raise

    logger.info(
        "Team checkout completed: org '%s' (slug=%s) created for user %s, Stripe customer %s",
        org_name,
        org.slug,
        user_id,
        stripe_customer_id,
    )


def _create_org_with_owner(
    user: User,
    org_name: str,
    *,
    stripe_customer_id: str | None = None,
    livemode: bool = False,
) -> tuple[Org, OrgMember]:
    """Atomically create an org, its owner membership, and (optionally) its Stripe customer.

    The user must already have account_type=ORG_MEMBER (set at registration).
    Passing `stripe_customer_id` links the org to its Stripe customer in the same
    transaction, preventing orgs without billing linkage on partial failure.
    """
    from apps.billing.models import StripeCustomer

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
        if stripe_customer_id is not None:
            StripeCustomer.objects.create(
                stripe_id=stripe_customer_id,
                org=org,
                livemode=livemode,
            )
    return org, member


async def deactivate_org(org_id: UUID) -> None:
    """Deactivate an org after its subscription is canceled.

    Sets is_active=False and cancels pending invitations.
    Called from the customer.subscription.deleted webhook handler.
    """
    updated = await Org.objects.filter(id=org_id, is_active=True).aupdate(is_active=False)
    if updated:
        await cancel_pending_invitations_for_org(org_id)
        logger.info("Deactivated org %s after subscription cancellation", org_id)
    else:
        logger.warning("Org %s already inactive or not found", org_id)


async def cancel_pending_invitations_for_org(org_id: UUID) -> int:
    """Cancel all pending invitations for an org. Returns count cancelled."""
    count = await Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING).aupdate(
        status=InvitationStatus.CANCELLED
    )
    return count


def accept_invitation(
    invitation: Invitation,
    *,
    password: str,
    full_name: str,
) -> tuple[User, Org]:
    """Create the invitee's user + membership and mark the invitation accepted.

    The invitation must already have been validated (not expired, org active,
    email not registered). Runs in a single transaction so a failure midway
    never leaves a dangling user, member, or accepted-but-unused invitation.
    """
    org = invitation.org
    with transaction.atomic():
        user = User.objects.create_user(
            email=invitation.email,
            password=password,
            full_name=full_name,
            account_type=AccountType.ORG_MEMBER,
            is_verified=True,  # trusted: invited by existing member
        )
        OrgMember.objects.create(
            org=org,
            user=user,
            role=invitation.role,
        )
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
    return user, org


def delete_org(org: Org) -> None:
    """Delete an org: cancel Stripe subs, hard-delete members and the org itself.

    DB work runs in a single atomic block; the Stripe cancellation is scheduled
    via on_commit so a Stripe failure cannot leave the DB partially deleted and
    a DB rollback cannot leave a dangling Stripe cancellation.
    """
    org_id = org.id
    with transaction.atomic():
        # Snapshot Stripe subscription IDs before deletion — StripeCustomer is
        # CASCADE-deleted with the org, so we must capture them first.
        stripe_sub_ids = [
            s.stripe_id for s in _get_active_stripe_subs(org_id) if s.stripe_id is not None
        ]

        async_to_sync(cancel_pending_invitations_for_org)(org_id)

        # Push the member_user_id set into the DB via a Subquery instead of
        # materializing thousands of UUIDs into Python for the IN clause.
        from django.db.models import Subquery

        member_user_ids_sq = OrgMember.objects.filter(org=org).values("user_id")
        User.objects.filter(id__in=Subquery(member_user_ids_sq)).delete()
        OrgMember.objects.filter(org=org).delete()

        org.delete()

        # Offload Stripe cancellations to Celery so the request returns
        # immediately instead of blocking on N sequential Stripe round-trips.
        if stripe_sub_ids:
            from apps.orgs.tasks import cancel_stripe_subs_task

            transaction.on_commit(
                lambda: cancel_stripe_subs_task.delay(stripe_sub_ids, str(org_id))
            )


def delete_orgs_created_by_user(user_id: UUID) -> None:
    """Delete all active orgs created by a user (used during account deletion)."""
    orgs = list(Org.objects.filter(created_by_id=user_id, deleted_at__isnull=True))
    for org in orgs:
        delete_org(org)


def _get_active_stripe_subs(org_id: UUID) -> list[SubscriptionModel]:
    """Return active Stripe-backed subscriptions for an org.

    Shared by seat decrement and subscription cancellation to avoid
    duplicating the StripeCustomer → Subscription lookup.
    """
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org_id=org_id)
    except StripeCustomer.DoesNotExist:
        return []

    return list(
        SubscriptionModel.objects.filter(
            stripe_customer=customer,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
    )


def decrement_subscription_seats(org_id: UUID) -> None:
    """Decrement the team subscription's seat count to match member count."""
    from saasmint_core.services.subscriptions import update_seat_count

    subs = _get_active_stripe_subs(org_id)
    if not subs:
        return

    sub = subs[0]
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
    except (stripe.StripeError, ValueError):
        logger.exception(
            "Failed to update seat count to %d for sub %s",
            new_quantity,
            sub.stripe_id,
        )


def _cancel_team_subscription(org: Org) -> None:
    """Cancel the team subscription for an org via Stripe (immediate cancellation)."""
    for sub in _get_active_stripe_subs(org.id):
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
