from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StripeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str  # evt_xxx — deduplication key
    type: str
    livemode: bool
    payload: dict  # type: ignore[type-arg]  # Stripe event payload is an untyped nested dict; dict[str, Any] would require importing Any
    processed_at: datetime | None = None
    error: str | None = None
    created_at: datetime
