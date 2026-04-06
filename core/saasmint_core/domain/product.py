from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProductType(StrEnum):
    ONE_TIME = "one_time"


class Product(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    type: ProductType
    credits: int
    is_active: bool = True


class ProductPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    product_id: UUID
    stripe_price_id: str
    currency: str
    amount: int  # minor units (cents)
