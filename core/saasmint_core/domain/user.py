from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: EmailStr
    full_name: str = Field(max_length=255)
    avatar_url: str | None = None
    preferred_locale: str = "en"
    preferred_currency: str = "usd"
    pronouns: str | None = None
    is_verified: bool = False
    created_at: datetime
    updated_at: datetime | None = None
