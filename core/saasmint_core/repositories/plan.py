from typing import Protocol

from saasmint_core.domain.subscription import Plan, PlanPrice


class PlanRepository(Protocol):
    async def list_active(self) -> list[Plan]: ...
    async def get_price_by_stripe_id(self, stripe_price_id: str) -> PlanPrice | None: ...
