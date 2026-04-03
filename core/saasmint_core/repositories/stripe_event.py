from typing import Protocol

from saasmint_core.domain.stripe_event import StripeEvent


class StripeEventRepository(Protocol):
    async def save(self, event: StripeEvent) -> StripeEvent: ...
    async def save_if_new(self, event: StripeEvent) -> bool:
        """Atomically save the event only if stripe_id doesn't exist yet.

        Returns True if the event was inserted (new), False if it already existed.
        """
        ...

    async def mark_processed(self, stripe_id: str) -> None: ...
    async def mark_failed(self, stripe_id: str, error: str) -> None: ...
    async def list_recent(self, limit: int = 50) -> list[StripeEvent]: ...
