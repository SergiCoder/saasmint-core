from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StripeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str  # evt_xxx — deduplication key
    type: str
    livemode: bool
    payload: dict[str, Any]
    processed_at: datetime | None = None
    error: str | None = None
    created_at: datetime
