"""Custom DRF exception handler — maps core domain errors to HTTP status codes."""

from __future__ import annotations

import logging
from typing import Any

import stripe
from rest_framework.response import Response
from rest_framework.views import exception_handler
from saasmint_core.exceptions import (
    AccountTypeConflictError,
    DomainError,
    InsufficientPermissionError,
    InvalidPromoCodeError,
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
    InvalidPromoCodeError: 422,
    InsufficientPermissionError: 403,
}


def domain_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Return an appropriate HTTP response for domain exceptions."""
    if isinstance(exc, DomainError):
        http_status = _STATUS_MAP.get(type(exc), 400)
        return Response({"detail": str(exc)}, status=http_status)

    if isinstance(exc, stripe.StripeError):
        logger.warning("Stripe API error: %s", exc)
        http_status = exc.http_status or 502
        return Response(
            {"detail": "Payment provider error. Please try again."},
            status=http_status,
        )

    return exception_handler(exc, context)
