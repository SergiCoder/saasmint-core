"""Billing app services — local subscription management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from saasmint_core.domain.subscription import FREE_SUBSCRIPTION_PERIOD_END

from apps.billing.models import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CreditBalance,
    CreditTransaction,
    Plan,
    PlanContext,
    Product,
    Subscription,
    SubscriptionStatus,
)
from apps.orgs.models import Org
from apps.users.models import AccountType, User

logger = logging.getLogger(__name__)


def _lock_user(user_id: UUID) -> None:
    """Take a row lock on the User row for *user_id* for the current txn.

    Used to serialize concurrent subscription-creation paths (register + OAuth
    link, webhook races) so two callers can't both see "no sub" and create
    duplicates. Must be called inside an ``atomic()`` block; the lock is held
    until commit. The fetched row itself is not needed.
    """
    # ``.only("id")`` keeps the SELECT ... FOR UPDATE narrow; we only need the
    # lock, not the full row. ``filter().first()`` silently no-ops for an
    # unknown user_id, which is the correct behavior for the races we guard —
    # the enclosing transaction will fail its subsequent FK check anyway.
    User.objects.select_for_update().only("id").filter(id=user_id).first()


def plan_context_for(user: User) -> PlanContext:
    """Return the PlanContext a user is billed under based on account type."""
    return PlanContext.TEAM if user.account_type == AccountType.ORG_MEMBER else PlanContext.PERSONAL


def get_active_team_subscription(org_id: UUID) -> Subscription | None:
    """Return the active team-billed Subscription for *org_id*, or None.

    Centralises the ``StripeCustomer→Subscription`` lookup used by seat-limit
    validation and decrement paths so the traversal stays in one place.
    """
    return (
        Subscription.objects.select_related("stripe_customer")
        .filter(
            stripe_customer__org_id=org_id,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
        .first()
    )


def assign_free_plan(user: User) -> None:
    """Create a free Subscription for *user*.

    Idempotent under concurrent register/OAuth races: the atomic+get_or_create
    pair prevents two parallel callers from both creating a free subscription.
    If no free plan exists, logs a WARNING and returns without creating a
    subscription — the user is left without a free fallback until a free plan
    is seeded.
    """
    free_plan = Plan.free_plans().first()
    if free_plan is None:
        logger.warning("No free plan found; skipping free subscription for user %s", user.id)
        return

    now = datetime.now(UTC)
    with transaction.atomic():
        # Lock the user row so two concurrent callers can't both see "no sub" and
        # create duplicates (register + OAuth-link can race at signup).
        _lock_user(user.id)
        # Any existing subscription row (paid or free, active or canceled)
        # blocks re-creation of the free fallback — callers are responsible
        # for cleaning up the old row first when upgrading/cancelling flows.
        if Subscription.objects.filter(user=user).exists():
            return
        Subscription.objects.create(
            user=user,
            status=SubscriptionStatus.ACTIVE,
            plan=free_plan,
            quantity=1,
            current_period_start=now,
            current_period_end=FREE_SUBSCRIPTION_PERIOD_END,
        )


def get_credit_balance(
    *,
    user: User | None = None,
    org: Org | None = None,
    org_id: UUID | None = None,
) -> int:
    """Return the current credit balance for a user or org (0 if none).

    Accepts ``org_id`` as a lightweight alternative to ``org`` so callers that
    already know the id (e.g. the credits view) don't have to hydrate the full
    ``Org`` row just to filter by FK.
    """
    provided = sum(x is not None for x in (user, org, org_id))
    if provided != 1:
        raise ValueError("Exactly one of user, org, or org_id must be provided.")
    if user is not None:
        row = CreditBalance.objects.filter(user=user).only("balance").first()
    elif org is not None:
        row = CreditBalance.objects.filter(org=org).only("balance").first()
    else:
        assert org_id is not None  # noqa: S101  (narrowed by `provided == 1` above)
        row = CreditBalance.objects.filter(org_id=org_id).only("balance").first()
    return row.balance if row is not None else 0


def grant_credits_for_session(
    *,
    stripe_session_id: str,
    amount: int,
    reason: str,
    user: User | None = None,
    org: Org | None = None,
) -> bool:
    """Grant *amount* credits to a user or org, keyed on a Stripe session id.

    Atomic + idempotent: inserts a ``CreditTransaction`` with unique
    ``stripe_session_id`` first; if the session was already processed the
    INSERT conflicts and we skip the balance update. Returns ``True`` when
    credits were granted this call, ``False`` when the session had already
    been processed (duplicate webhook delivery).
    """
    if (user is None) == (org is None):
        raise ValueError("Exactly one of user or org must be provided.")
    if amount <= 0:
        raise ValueError("grant_credits_for_session requires a positive amount.")

    with transaction.atomic():
        try:
            CreditTransaction.objects.create(
                user=user,
                org=org,
                amount=amount,
                reason=reason,
                stripe_session_id=stripe_session_id,
            )
        except IntegrityError:
            logger.info(
                "Credit grant for session %s already processed — skipping", stripe_session_id
            )
            return False

        if user is not None:
            balance, _ = CreditBalance.objects.select_for_update().get_or_create(
                user=user, defaults={"balance": 0}
            )
        else:
            balance, _ = CreditBalance.objects.select_for_update().get_or_create(
                org=org, defaults={"balance": 0}
            )
        balance.balance += amount
        balance.save(update_fields=["balance", "updated_at"])
        return True


async def on_product_checkout_completed(
    stripe_session_id: str,
    product_id: UUID,
    user_id: UUID,
    org_id: UUID | None,
) -> None:
    """Grant credits for a completed product checkout (webhook callback).

    Looks up the ``Product`` to find the credit count, resolves the owner
    (org when ``org_id`` is set, otherwise the user who initiated checkout),
    and delegates to :func:`grant_credits_for_session` for the atomic grant.
    """

    def _grant() -> None:
        try:
            product = Product.objects.only("credits", "name").get(id=product_id)
        except Product.DoesNotExist:
            logger.warning(
                "Product checkout session %s references unknown product %s",
                stripe_session_id,
                product_id,
            )
            return
        if product.credits <= 0:
            logger.warning(
                "Product checkout session %s grants zero credits (product=%s) — skipping",
                stripe_session_id,
                product.name,
            )
            return

        if org_id is not None:
            org = Org.objects.only("id").filter(id=org_id).first()
            if org is None:
                logger.warning(
                    "Product checkout session %s references unknown org %s",
                    stripe_session_id,
                    org_id,
                )
                return
            grant_credits_for_session(
                stripe_session_id=stripe_session_id,
                amount=product.credits,
                reason=f"purchase:{product.name}",
                org=org,
            )
        else:
            user = User.objects.only("id").filter(id=user_id).first()
            if user is None:
                logger.warning(
                    "Product checkout session %s references unknown user %s",
                    stripe_session_id,
                    user_id,
                )
                return
            grant_credits_for_session(
                stripe_session_id=stripe_session_id,
                amount=product.credits,
                reason=f"purchase:{product.name}",
                user=user,
            )

    await sync_to_async(_grant)()
