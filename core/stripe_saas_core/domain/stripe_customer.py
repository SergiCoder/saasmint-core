from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class StripeCustomer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str  # cus_xxx
    user_id: UUID | None = None
    org_id: UUID | None = None
    livemode: bool = False
    created_at: datetime

    @model_validator(mode="after")
    def exactly_one_owner(self) -> Self:
        has_user = self.user_id is not None
        has_org = self.org_id is not None
        if has_user == has_org:
            raise ValueError("exactly one of user_id or org_id must be set")
        return self
