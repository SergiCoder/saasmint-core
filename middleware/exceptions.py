"""Custom DRF exception handler — maps core domain errors to HTTP status codes."""

from __future__ import annotations

import logging
import re
from typing import Any

import stripe
from rest_framework.response import Response
from rest_framework.views import exception_handler
from saasmint_core.exceptions import (
    AccountTypeConflictError,
    DomainError,
    InsufficientPermissionError,
    OrgMemberNotFoundError,
    OrgNotFoundError,
    SubscriptionAlreadyActiveError,
    SubscriptionNotFoundError,
    UserNotFoundError,
)

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[type[DomainError], int] = {
    UserNotFoundError: 404,
    OrgNotFoundError: 404,
    OrgMemberNotFoundError: 404,
    SubscriptionNotFoundError: 404,
    SubscriptionAlreadyActiveError: 409,
    AccountTypeConflictError: 409,
    InsufficientPermissionError: 403,
}


def _code_for(exc_cls: type[Exception]) -> str:
    """Derive a snake_case error code from an exception class name.

    ``OrgNotFoundError`` → ``"org_not_found"``.
    """
    name = exc_cls.__name__.removesuffix("Error")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def domain_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Return an appropriate HTTP response for domain exceptions."""
    if isinstance(exc, DomainError):
        http_status = _STATUS_MAP.get(type(exc), 400)
        return Response(
            {"detail": str(exc), "code": _code_for(type(exc))},
            status=http_status,
        )

    if isinstance(exc, stripe.StripeError):
        logger.warning("Stripe API error: %s", exc)
        http_status = exc.http_status or 502
        return Response(
            {
                "detail": "Payment provider error. Please try again.",
                "code": "payment_provider_error",
            },
            status=http_status,
        )

    return exception_handler(exc, context)
