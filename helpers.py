"""Shared helpers for the Django backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models
from rest_framework.request import Request

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.users.models import User


def get_user(request: Request) -> User:
    """Extract the authenticated user from a DRF request with correct typing."""
    return request.user  # type: ignore[return-value]


async def aget_or_none[T](
    model_class: type[models.Model],
    to_domain: Callable[..., T],
    **kwargs: Any,  # noqa: ANN401
) -> T | None:
    """Fetch a single ORM object and convert to domain, or return None."""
    try:
        obj = await model_class.objects.aget(**kwargs)  # type: ignore[attr-defined]
        return to_domain(obj)
    except model_class.DoesNotExist:  # type: ignore[attr-defined]
        return None
