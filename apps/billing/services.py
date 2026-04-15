"""Billing app services — local subscription management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from django.db import transaction
from saasmint_core.domain.subscription import FREE_SUBSCRIPTION_PERIOD_END

from apps.billing.models import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    Plan,
    PlanContext,
    Subscription,
    SubscriptionStatus,
)
from apps.users.models import AccountType, User

logger = logging.getLogger(__name__)


def plan_context_for(user: User) -> PlanContext:
    """Return the PlanContext a user is billed under based on account type."""
    return (
        PlanContext.TEAM
        if user.account_type == AccountType.ORG_MEMBER
        else PlanContext.PERSONAL
    )


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
    Does nothing if no free plan exists in the database.
    """
    free_plan = Plan.free_plans().first()
    if free_plan is None:
        logger.warning("No free plan found; skipping free subscription for user %s", user.id)
        return

    now = datetime.now(UTC)
    with transaction.atomic():
        # Lock the user row so two concurrent callers can't both see "no sub" and
        # create duplicates (register + OAuth-link can race at signup).
        User.objects.select_for_update().filter(id=user.id).first()
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
