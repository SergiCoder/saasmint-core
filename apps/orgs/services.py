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

    Race semantics: this is a best-effort generator, not a guarantee. The
    scan + pick is not transactional, so two concurrent callers can land on
    the same candidate. The partial unique index on `Org.slug` where
    `deleted_at IS NULL` (see `idx_orgs_slug_active`) is the authoritative
    uniqueness enforcer — callers are expected to wrap the `Org.create()`
    in a try/except for `IntegrityError` and retry if they must survive a
    lost race (see `_create_org_with_owner`).
    """
    base = slugify(name)
    # Strip any characters not in [a-z0-9-]
    base = re.sub(r"[^a-z0-9-]", "", base)
    # Strip leading/trailing hyphens
    base = base.strip("-")
    # Ensure minimum length
    if len(base) < 2:
        base = "org"

    # Pull candidate variants in one query (`base`, `base-2`, `base-3`, ...)
    # using a ``startswith`` scan so the ``idx_orgs_slug_active`` partial index
    # can seek the prefix — ``slug__regex`` was opaque to the planner and
    # fell back to a full-table scan. Filter to exact-match or ``-<digits>``
    # in Python; anything else (e.g. ``foo-bar`` when base=``foo``) is
    # discarded, so the wider candidate set is harmless.
    _suffix_re = re.compile(rf"^{re.escape(base)}(?:-\d+)?$")
    existing = {
        slug
        for slug in Org.objects.filter(
            slug__startswith=base,
            deleted_at__isnull=True,
        ).values_list("slug", flat=True)
        if _suffix_re.match(slug)
    }
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

    Handles the user→org transition: ``CheckoutSessionView`` saves a user-scoped
    ``StripeCustomer`` when starting team checkout (the org does not exist yet).
    On ``checkout.session.completed``, that same row is re-bound here to the new
    org. A ``StripeCustomer`` already linked to an org is treated as a duplicate
    webhook delivery — the existing org and membership are returned unchanged.
    """
    from apps.billing.models import StripeCustomer

    if user.account_type != AccountType.ORG_MEMBER:
        raise ValueError(f"User {user.id} must have account_type=org_member to create an org")

    with transaction.atomic():
        # Duplicate-webhook short-circuit has to happen INSIDE the transaction
        # with SELECT FOR UPDATE — otherwise two concurrent deliveries can
        # both pass a pre-check, both create an Org, and the second wins the
        # StripeCustomer reassignment, orphaning the first Org.
        existing_customer = None
        if stripe_customer_id is not None:
            existing_customer = (
                StripeCustomer.objects.select_for_update()
                .filter(stripe_id=stripe_customer_id)
                .first()
            )
            if existing_customer is not None and existing_customer.org_id is not None:
                already_org = Org.objects.filter(id=existing_customer.org_id).first()
                if already_org is not None:
                    member = OrgMember.objects.filter(org=already_org, user=user).first()
                    if member is not None:
                        return already_org, member

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
            if existing_customer is None:
                StripeCustomer.objects.create(
                    stripe_id=stripe_customer_id,
                    org=org,
                    livemode=livemode,
                )
            else:
                existing_customer.user = None
                existing_customer.org = org
                existing_customer.livemode = livemode
                existing_customer.save(update_fields=["user", "org", "livemode"])
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

    The user is created with ``is_verified=False`` — a verification email is
    queued on commit so the invitee must prove mailbox control before they
    can log in. This blocks a leaked/forwarded invitation token from
    silently onboarding an attacker, since they cannot click the verify
    link that lands in the real invitee's inbox.
    """
    from apps.users.authentication import create_email_verification_token
    from apps.users.tasks import send_verification_email_task

    org = invitation.org
    with transaction.atomic():
        user = User.objects.create_user(
            email=invitation.email,
            password=password,
            full_name=full_name,
            account_type=AccountType.ORG_MEMBER,
            is_verified=False,
        )
        OrgMember.objects.create(
            org=org,
            user=user,
            role=invitation.role,
        )
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        verification_token = create_email_verification_token(user)
        transaction.on_commit(
            lambda: send_verification_email_task.delay(user.email, verification_token)
        )
    return user, org


def _delete_org_db_only(org: Org) -> None:
    """Delete an org's DB state (invitations, members, users, the org row).

    No Stripe cancellation — the caller owns the fan-out, so it can either
    schedule one task per org (:func:`delete_org`) or batch one task across
    many orgs (:func:`delete_orgs_created_by_user`).
    """
    from django.db.models import Exists, OuterRef, Subquery

    org_id = org.id
    with transaction.atomic():
        # Inline sync UPDATE — the caller already runs in a sync transaction,
        # so bouncing through async_to_sync to call the async helper would
        # just wrap the same UPDATE in an event loop for no reason.
        Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING).update(
            status=InvitationStatus.CANCELLED
        )

        # Delete only users whose *only* membership is in this org — users
        # who also belong to another org must keep their account, otherwise
        # deleting org A would wipe accounts still active in org B.
        # The NOT EXISTS subquery is evaluated in the DB so we don't need to
        # materialize thousands of UUIDs into Python for the IN clause.
        other_memberships = OrgMember.objects.filter(user_id=OuterRef("user_id")).exclude(
            org_id=org_id
        )
        single_org_member_user_ids = (
            OrgMember.objects.filter(org=org)
            .annotate(has_other=Exists(other_memberships))
            .filter(has_other=False)
            .values("user_id")
        )
        User.objects.filter(id__in=Subquery(single_org_member_user_ids)).delete()
        OrgMember.objects.filter(org=org).delete()

        org.delete()


def delete_org(org: Org) -> None:
    """Delete an org: cancel its Stripe sub, hard-delete members and the org itself.

    DB work runs in a single atomic block; the Stripe cancellation is scheduled
    via on_commit so a Stripe failure cannot leave the DB partially deleted and
    a DB rollback cannot leave a dangling Stripe cancellation.
    """
    from apps.orgs.tasks import cancel_stripe_subs_task

    org_id = org.id
    # Snapshot the Stripe subscription ID before deletion — StripeCustomer is
    # CASCADE-deleted with the org, so we must capture it first.
    active_sub = _get_active_stripe_sub(org_id)
    stripe_sub_id = active_sub.stripe_id if active_sub is not None else None

    _delete_org_db_only(org)

    # Offload Stripe cancellation to Celery so the request returns
    # immediately instead of blocking on the Stripe round-trip.
    if stripe_sub_id is not None:
        transaction.on_commit(lambda: cancel_stripe_subs_task.delay([stripe_sub_id], str(org_id)))


def delete_orgs_created_by_user(user_id: UUID) -> None:
    """Delete every active org created by *user_id* (used during account deletion).

    Collects every org's active Stripe subscription first, then fires one
    batched ``cancel_stripe_subs_task`` with all the IDs instead of dispatching
    one Celery message per org. The cancel task already accepts a list, so
    the behavior is unchanged — we just avoid K broker round-trips for a user
    who created K orgs.
    """
    from apps.orgs.tasks import cancel_stripe_subs_task

    orgs = list(Org.objects.filter(created_by_id=user_id, deleted_at__isnull=True))
    if not orgs:
        return

    pending_stripe_sub_ids: list[str] = []
    for org in orgs:
        sub = _get_active_stripe_sub(org.id)
        if sub is not None and sub.stripe_id is not None:
            pending_stripe_sub_ids.append(sub.stripe_id)
        _delete_org_db_only(org)

    if pending_stripe_sub_ids:
        # No single org_id owns the batch — pass the caller's user_id instead
        # so failures can still be traced back to the originating delete.
        transaction.on_commit(
            lambda: cancel_stripe_subs_task.delay(pending_stripe_sub_ids, f"user:{user_id}")
        )


def _get_active_stripe_sub(org_id: UUID) -> SubscriptionModel | None:
    """Return the active Stripe-backed subscription for an org, or None.

    Each org holds at most one active Stripe subscription at a time — the
    singular return makes that invariant explicit. If multiple active rows
    exist (sync-window drift, duplicate webhook), the newest wins.
    """
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel

    return (
        SubscriptionModel.objects.filter(
            stripe_customer__org_id=org_id,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
        .order_by("-created_at")
        .first()
    )


def decrement_subscription_seats(org_id: UUID) -> None:
    """Decrement the team subscription's seat count to match member count."""
    from saasmint_core.services.subscriptions import update_seat_count

    sub = _get_active_stripe_sub(org_id)
    if sub is None or sub.stripe_id is None:
        return

    # Lock the OrgMember rows while we compute the new seat count so two
    # concurrent member removals can't both read the pre-decrement total
    # and then push the same (stale) count to Stripe. Snapshot the count
    # inside the txn and push to Stripe only after commit to avoid holding
    # DB locks across the external API call.
    with transaction.atomic():
        new_quantity = (
            OrgMember.objects.select_for_update()
            .filter(org_id=org_id, org__deleted_at__isnull=True)
            .count()
        )

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
    sub = _get_active_stripe_sub(org.id)
    if sub is None or sub.stripe_id is None:
        return
    try:
        stripe.Subscription.cancel(sub.stripe_id)
    except stripe.StripeError:
        logger.exception(
            "Failed to cancel Stripe sub %s for org %s",
            sub.stripe_id,
            org.id,
        )
