"""Billing app services — local subscription management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from saasmint_core.domain.subscription import FREE_SUBSCRIPTION_PERIOD_END

from apps.billing.models import Plan, Subscription, SubscriptionStatus
from apps.users.models import User

logger = logging.getLogger(__name__)


def _get_free_plan() -> Plan | None:
    """Return the active personal plan where every price is $0, or None."""
    return (
        Plan.objects.filter(is_active=True, context="personal", price__amount=0)
        .select_related("price")
        .first()
    )


def assign_free_plan(user: User) -> None:
    """Create a free Subscription for *user*.

    Idempotent: skips if the user already has any subscription.
    Does nothing if no free plan exists in the database.
    """
    free_plan = _get_free_plan()
    if free_plan is None:
        logger.warning("No free plan found; skipping free subscription for user %s", user.id)
        return

    if Subscription.objects.filter(user=user).exists():
        return

    now = datetime.now(UTC)
    Subscription.objects.create(
        user=user,
        status=SubscriptionStatus.ACTIVE,
        plan=free_plan,
        quantity=1,
        current_period_start=now,
        current_period_end=FREE_SUBSCRIPTION_PERIOD_END,
    )
