"""Organization lifecycle services — team checkout, member management, invitations.

Note on the ``apps.billing.*`` imports inside function bodies: ``apps.billing``
imports ``apps.orgs.models.OrgMember`` at module top (e.g. for the
billing-authority gates and credit routing in ``apps.billing.views``). A
top-level ``from apps.billing.models import …`` here would close that loop
at startup. The lazy function-scope imports below are deliberate — they keep
the cycle deferred to call time, where Python has finished importing both
modules. Don't promote any of them to module top without first auditing the
billing → orgs side.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from apps.billing.models import Subscription as SubscriptionModel

import stripe
from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.utils.text import slugify
from saasmint_core.exceptions import DomainError

from apps.orgs.models import Invitation, InvitationStatus, Org, OrgMember, OrgRole
from apps.users.models import User

logger = logging.getLogger(__name__)


def generate_unique_slug(name: str) -> str:
    """Generate a unique org slug from a name.

    Slugifies the name, ensures it matches [a-z0-9][a-z0-9-]*[a-z0-9] (min 2 chars),
    and appends a numeric suffix if the slug is already taken.

    Race semantics: this is a best-effort generator, not a guarantee. The
    scan + pick is not transactional, so two concurrent callers can land on
    the same candidate. The field-level unique index on ``Org.slug`` is the
    authoritative uniqueness enforcer — callers are expected to wrap the
    ``Org.create()`` in a try/except for ``IntegrityError`` and retry if
    they must survive a lost race (see ``_create_org_with_owner``).
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
    # using a ``startswith`` scan so the field-level unique index on
    # ``Org.slug`` can seek the prefix — ``slug__regex`` was opaque to the
    # planner and fell back to a full-table scan. Filter to exact-match or
    # ``-<digits>`` in Python; anything else (e.g. ``foo-bar`` when
    # base=``foo``) is discarded, so the wider candidate set is harmless.
    _suffix_re = re.compile(rf"^{re.escape(base)}(?:-\d+)?$")
    existing = {
        slug
        for slug in Org.objects.filter(
            slug__startswith=base,
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
    keep_personal_subscription: bool,
) -> None:
    """Create an org and its Stripe customer after a team plan checkout.

    Called from the checkout.session.completed webhook handler. Org membership
    is the only signal that distinguishes a team-billing user from a personal
    one — successful team checkout creates the OrgMember row.

    When ``keep_personal_subscription`` is False (the default for the upgrade
    flow), the user's existing personal subscription — if any — is scheduled
    to cancel at period end so they're not double-billed (rule 16). Set True
    to leave the personal sub running concurrently with the new team sub
    (rule 5b).
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

    if stripe_subscription_id is not None:
        await _persist_team_subscription(stripe_subscription_id)

    if not keep_personal_subscription:
        await _schedule_personal_cancel_at_period_end(user_id)

    logger.info(
        "Team checkout completed: org '%s' (slug=%s) created for user %s, Stripe customer %s",
        org_name,
        org.slug,
        user_id,
        stripe_customer_id,
    )


async def _persist_team_subscription(stripe_subscription_id: str) -> None:
    """Upsert the team subscription mirror row immediately after org creation.

    Stripe sometimes delivers ``customer.subscription.created`` BEFORE
    ``checkout.session.completed`` — when that happens, the sync webhook
    raises ``Unknown customer`` because the ``StripeCustomer`` row has
    not been written yet (it's only created here, by the team-checkout
    handler). Fetching the subscription from Stripe and upserting it now
    closes that race; the later ``customer.subscription.updated`` events
    flow through the same idempotent upsert.
    """
    from saasmint_core.services.webhooks import sync_subscription_from_data

    from apps.billing.repositories import get_webhook_repos

    sub = await asyncio.to_thread(stripe.Subscription.retrieve, stripe_subscription_id)
    repos = get_webhook_repos()
    # ``stripe.Subscription.retrieve`` returns a ``StripeObject``: it supports
    # ``[...]`` indexing but does NOT inherit from ``dict``, so ``.get(...)``,
    # ``.items()``, etc. are proxied through ``__getattr__`` and crash with
    # ``AttributeError`` when the requested key is absent. The webhook-dispatch
    # path receives plain dicts (json-decoded by ``stripe.Webhook.construct_event``)
    # and ``sync_subscription_from_data`` is written against dict semantics, so
    # we normalise at the boundary via ``to_dict()`` (recursive in this SDK).
    await sync_subscription_from_data(
        sub.to_dict(),
        customers=repos.customers,
        plans=repos.plans,
        subscriptions=repos.subscriptions,
    )


async def _schedule_personal_cancel_at_period_end(user_id: UUID) -> None:
    """Schedule cancel-at-period-end on the user's personal sub, if any.

    No-op when the user has no user-scoped Stripe customer or no active
    personal subscription. Idempotent on already-scheduled subs — Stripe's
    ``Subscription.modify(cancel_at="min_period_end")`` accepts being called
    repeatedly with the same value.
    """
    from saasmint_core.exceptions import SubscriptionNotFoundError
    from saasmint_core.services.billing import cancel_subscription

    from apps.billing.repositories import get_billing_repos

    repos = get_billing_repos()
    personal_customer = await repos.customers.get_by_user_id(user_id)
    if personal_customer is None:
        return

    try:
        await cancel_subscription(
            stripe_customer_id=personal_customer.id,
            at_period_end=True,
            subscription_repo=repos.subscriptions,
        )
    except SubscriptionNotFoundError:
        # User has a personal customer but no active sub on it — fine, nothing to cancel.
        return


def _create_org_with_owner(
    user: User,
    org_name: str,
    *,
    stripe_customer_id: str | None = None,
    livemode: bool = False,
) -> tuple[Org, OrgMember]:
    """Atomically create an org, its owner membership, and its Stripe customer.

    All three state changes happen in a single transaction so partial-failure
    can't leave an org without billing linkage. The OrgMember row is the
    authoritative signal that this user is now an org member — no separate
    flag on User is needed.

    Duplicate-webhook short-circuit: a ``StripeCustomer`` row that already
    points to an org+OrgMember pair indicates a re-delivery and returns the
    existing org+member unchanged.
    """
    from apps.billing.models import StripeCustomer

    with transaction.atomic():
        # Duplicate-webhook short-circuit has to happen INSIDE the transaction
        # with SELECT FOR UPDATE — otherwise two concurrent deliveries can
        # both pass a pre-check, both create an Org, and the second wins the
        # StripeCustomer creation, orphaning the first Org.
        if stripe_customer_id is not None:
            existing = (
                StripeCustomer.objects.select_for_update()
                .filter(stripe_id=stripe_customer_id)
                .first()
            )
            if existing is not None and existing.org_id is not None:
                already_org = Org.objects.filter(id=existing.org_id).first()
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
            StripeCustomer.objects.create(
                stripe_id=stripe_customer_id,
                org=org,
                livemode=livemode,
            )

    return org, member


async def delete_org_on_subscription_cancel(org_id: UUID) -> None:
    """Schedule hard-delete of an org after its team subscription is canceled.

    Dispatch-only: the cascade itself runs in
    :func:`apps.orgs.tasks.delete_org_on_subscription_cancel_task` so the
    Stripe webhook handler returns within the retry window. See that task
    for the cascade semantics, idempotency contract, and the rule-9 note on
    why we don't branch on ``cancellation_details.reason``.
    """
    from apps.orgs.tasks import delete_org_on_subscription_cancel_task

    delete_org_on_subscription_cancel_task.delay(str(org_id))


class SeatCapReachedAtAcceptError(DomainError):
    """Raised by ``accept_invitation`` when the team's seat cap is reached at commit.

    Re-raised by the view layer as a 409 Conflict so the invitee gets a
    clean error rather than an opaque IntegrityError. Distinct exception
    type so callers can distinguish it from generic IntegrityError races.
    """


def _lock_active_team_sub(org: Org) -> SubscriptionModel | None:
    """Return the org's active team Subscription with a row lock, or ``None``.

    Must be called inside an ``atomic()`` block — the ``SELECT FOR UPDATE``
    is held until the surrounding transaction commits, serialising any
    concurrent invite-create / invite-accept / member-add against the same
    org so the seat cap can't be overrun in a TOCTOU race.

    Shared by ``_validate_seat_limit`` (invite create, in views) and
    ``accept_invitation`` (invite accept, here). Each call site keeps its
    own post-lock counting logic — invite-create counts members + pending
    invites, invite-accept counts members + 1 — so this helper only owns
    the lock query, not the cap math.
    """
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel

    return (
        SubscriptionModel.objects.select_for_update()
        .filter(
            stripe_customer__org=org,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
        .first()
    )


def accept_invitation(
    invitation: Invitation,
    *,
    full_name: str,
) -> tuple[User, Org]:
    """Create the invitee's user + membership and mark the invitation accepted.

    The invitation must already have been validated (not expired, org active,
    email not registered, seat cap not reached). Runs in a single transaction
    so a failure midway never leaves a dangling user, member, or
    accepted-but-unused invitation.

    The user is created with ``set_unusable_password()`` — the password is
    set later by the invitee through the verification-email flow
    (``POST /api/v1/auth/verify-email/`` with both the token and a fresh
    password). This decouples credential setting from the invitation token:
    a leaked/forwarded accept link cannot bind an attacker-chosen password,
    because the only way to set the password is to consume a verification
    token that's only ever delivered to the invitee's inbox.

    The seat-limit check is re-run inside this transaction with a row-lock
    on the team Subscription so a leaked-token race against a parallel
    membership change cannot push the org past its seat cap at commit time.
    """
    from django.db.models import Count

    from apps.users.authentication import create_email_verification_token
    from apps.users.tasks import send_verification_email_task

    org = invitation.org
    with transaction.atomic():
        # Lock the active team sub row first so any concurrent invitation
        # accept / invite create / member add against the same org serialises
        # behind us. Mirrors the lock taken at invite-creation time in
        # ``_validate_seat_limit``.
        sub = _lock_active_team_sub(org)
        if sub is not None:
            counts = Org.objects.filter(pk=org.pk).aggregate(
                member_count=Count("members", distinct=True),
            )
            if counts["member_count"] + 1 > sub.seat_limit:
                raise SeatCapReachedAtAcceptError(
                    "This invitation cannot be accepted: the org has filled"
                    " every seat on its current subscription. Ask an admin to"
                    " expand the plan."
                )

        # ``UserManager.create_user`` calls ``set_unusable_password()`` when
        # ``password`` is None — the invitee binds a real password later via
        # the verify-email flow.
        user = User.objects.create_user(
            email=invitation.email,
            password=None,
            full_name=full_name,
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


def _personal_subs_outer_ref_qs() -> QuerySet[SubscriptionModel]:
    """Subquery: rows in ``Subscription`` representing an active personal sub for
    the current outer ``user_id``. Used in the cascade-delete predicates that
    must preserve users still paying for their own plan. Lazy-imports the
    billing model to keep ``apps.orgs`` from depending on ``apps.billing`` at
    module load (see file-level docstring).
    """
    from django.db.models import OuterRef

    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel

    return SubscriptionModel.objects.filter(
        user_id=OuterRef("user_id"),
        stripe_customer__user_id=OuterRef("user_id"),
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
    )


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

        # Delete only users whose *only* membership is in this org **and**
        # who don't have an active personal subscription — otherwise
        # deleting org A would wipe accounts still active in org B, or
        # silently nuke a user who's still paying for their own personal plan.
        # The NOT EXISTS subqueries are evaluated in the DB so we don't need
        # to materialize thousands of UUIDs into Python for the IN clause.
        other_memberships = OrgMember.objects.filter(user_id=OuterRef("user_id")).exclude(
            org_id=org_id
        )
        personal_subs = _personal_subs_outer_ref_qs()
        deletable_user_ids = (
            OrgMember.objects.filter(org=org)
            .annotate(
                has_other=Exists(other_memberships),
                has_personal_sub=Exists(personal_subs),
            )
            .filter(has_other=False, has_personal_sub=False)
            .values("user_id")
        )
        User.objects.filter(id__in=Subquery(deletable_user_ids)).delete()
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

    DB cascade for the K orgs runs as a single transaction with one DML per
    table (Invitation UPDATE, User DELETE for users whose only memberships
    were inside this batch and who have no active personal sub, OrgMember
    DELETE, Org DELETE) — saves 4(K-1) round-trips compared to calling
    :func:`_delete_org_db_only` once per org.
    """
    from django.db.models import Exists, OuterRef, Subquery

    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel
    from apps.orgs.tasks import cancel_stripe_subs_task

    orgs = list(Org.objects.filter(created_by_id=user_id))
    if not orgs:
        return

    # One IN-query for active subs across every org, ordered so the first
    # match per org_id is the latest. Avoids K round-trips for a user who
    # created K orgs.
    org_ids = [org.id for org in orgs]
    sub_by_org: dict[UUID, SubscriptionModel] = {}
    for sub in (
        SubscriptionModel.objects.filter(
            stripe_customer__org_id__in=org_ids,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
        .select_related("stripe_customer")
        .order_by("-created_at")
    ):
        # Filter pins ``stripe_customer__org_id`` to one of ``org_ids`` so the
        # join row exists and ``org_id`` is non-null — mypy doesn't see that.
        org_id = sub.stripe_customer.org_id  # type: ignore[union-attr]
        if org_id is not None and org_id not in sub_by_org:
            sub_by_org[org_id] = sub

    pending_stripe_sub_ids: list[str] = []
    for org in orgs:
        org_sub = sub_by_org.get(org.id)
        if org_sub is not None and org_sub.stripe_id is not None:
            pending_stripe_sub_ids.append(org_sub.stripe_id)

    # Batched DB cascade: one transaction, one DML per table. Survival rule
    # mirrors :func:`_delete_org_db_only` — a user is deleted only when they
    # have no membership outside this batch AND no active personal sub.
    with transaction.atomic():
        Invitation.objects.filter(
            org_id__in=org_ids, status=InvitationStatus.PENDING
        ).update(status=InvitationStatus.CANCELLED)

        other_memberships = OrgMember.objects.filter(user_id=OuterRef("user_id")).exclude(
            org_id__in=org_ids
        )
        personal_subs = _personal_subs_outer_ref_qs()
        deletable_user_ids = (
            OrgMember.objects.filter(org_id__in=org_ids)
            .annotate(
                has_other=Exists(other_memberships),
                has_personal_sub=Exists(personal_subs),
            )
            .filter(has_other=False, has_personal_sub=False)
            .values("user_id")
            .distinct()
        )
        User.objects.filter(id__in=Subquery(deletable_user_ids)).delete()
        OrgMember.objects.filter(org_id__in=org_ids).delete()
        Org.objects.filter(id__in=org_ids).delete()

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


