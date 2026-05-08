from typing import Protocol

from saasmint_core.domain.product import Product


class ProductRepository(Protocol):
    async def list_active(self) -> list[Product]: ...
