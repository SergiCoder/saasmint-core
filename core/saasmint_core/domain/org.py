from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OrgRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Org(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    slug: str
    logo_url: str | None = None
    is_active: bool = True
    created_by: UUID
    created_at: datetime
    deleted_at: datetime | None = None


class OrgMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    org_id: UUID
    user_id: UUID
    role: OrgRole
    is_billing: bool = False
    joined_at: datetime


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DECLINED = "declined"


class Invitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    org_id: UUID
    email: str
    role: OrgRole
    token: str
    status: InvitationStatus = InvitationStatus.PENDING
    invited_by: UUID
    created_at: datetime
    expires_at: datetime
