from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PENDING = "pending"
    FAILED = "failed"
    CANCELED = "canceled"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str
    stripe_customer_id: UUID
    amount: int  # minor units
    currency: str
    status: PaymentStatus
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class Invoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    stripe_id: str
    stripe_customer_id: UUID
    subscription_id: UUID | None = None
    amount_due: int
    amount_paid: int
    currency: str
    status: InvoiceStatus
    hosted_url: str | None = None
    pdf_url: str | None = None
    due_date: datetime | None = None
    created_at: datetime
