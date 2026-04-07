from datetime import datetime
from enum import StrEnum
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


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    description: str = ""
    context: PlanContext
    interval: PlanInterval
    is_active: bool = True


class PlanPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    plan_id: UUID
    stripe_price_id: str
    amount: int  # USD cents


class Subscription(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str | None = None
    stripe_customer_id: UUID | None = None
    user_id: UUID | None = None
    status: SubscriptionStatus
    plan_id: UUID
    quantity: int = 1
    promotion_code_id: str | None = None
    discount_percent: float | None = None
    discount_end_at: datetime | None = None
    trial_ends_at: datetime | None = None
    current_period_start: datetime
    current_period_end: datetime
    canceled_at: datetime | None = None
    created_at: datetime

    @property
    def is_free(self) -> bool:
        """True when this subscription has no Stripe backing (free plan)."""
        return self.stripe_id is None
