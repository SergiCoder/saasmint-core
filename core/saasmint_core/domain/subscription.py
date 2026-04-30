from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"
    UNPAID = "unpaid"


ACTIVE_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.PAST_DUE,
    }
)


class PlanInterval(StrEnum):
    MONTH = "month"
    YEAR = "year"


class PlanContext(StrEnum):
    PERSONAL = "personal"
    TEAM = "team"


class PlanTier(IntEnum):
    FREE = 1
    BASIC = 2
    PRO = 3


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    description: str = ""
    context: PlanContext
    tier: PlanTier = PlanTier.BASIC
    interval: PlanInterval
    is_active: bool = True


class PlanPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    plan_id: UUID
    stripe_price_id: str
    amount: int  # USD cents


class Subscription(BaseModel):
    """Local mirror of a Stripe subscription.

    The free tier is the *absence* of a Subscription — there are no rows
    without a stripe_id. ``stripe_id``, ``stripe_customer_id``, and
    ``user_id`` are still typed as nullable to keep the domain model
    permissive for in-flight construction (webhook handlers build the
    object incrementally), but a persisted row always has at least one
    owner reference and a stripe_id.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str | None = None
    stripe_customer_id: UUID | None = None
    user_id: UUID | None = None
    status: SubscriptionStatus
    plan_id: UUID
    quantity: int = 1
    trial_ends_at: datetime | None = None
    current_period_start: datetime
    current_period_end: datetime
    canceled_at: datetime | None = None
    cancel_at: datetime | None = None
    created_at: datetime
