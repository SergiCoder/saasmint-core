"""Shared helpers for the Django backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Manager
from rest_framework.request import Request

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.users.models import User


def get_user(request: Request) -> User:
    """Extract the authenticated user from a DRF request with correct typing."""
    from apps.users.models import User as UserModel

    return cast(UserModel, request.user)


async def aget_or_none[T](
    model_class: type[models.Model],
    to_domain: Callable[..., T],
    **kwargs: Any,  # noqa: ANN401  # kwargs forwarded to aget(); heterogeneous ORM filter args by design
) -> T | None:
    """Fetch a single ORM object and convert to domain, or return None."""
    manager: Manager[models.Model] = model_class._default_manager
    try:
        obj = await manager.aget(**kwargs)
        return to_domain(obj)
    except ObjectDoesNotExist:
        return None
