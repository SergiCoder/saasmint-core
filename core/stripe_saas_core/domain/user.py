from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class AccountType(StrEnum):
    PERSONAL = "personal"
    ORG_MEMBER = "org_member"


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    supabase_uid: str
    email: EmailStr
    full_name: str | None = None
    avatar_url: str | None = None
    account_type: AccountType = AccountType.PERSONAL
    preferred_locale: str = "en"
    preferred_currency: str = "usd"
    is_verified: bool = False
    created_at: datetime
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
