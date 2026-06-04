from typing import Protocol
from uuid import UUID

from saasmint_core.domain.user import User


class UserRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...
    async def save(self, user: User) -> User: ...
    async def hard_delete(self, user_id: UUID) -> None: ...
