"""Shared DRF base views — permission and throttle scaffolding reused across apps."""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView


def paginated_response_schema(
    name: str, child: drf_serializers.BaseSerializer[Any]
) -> drf_serializers.Serializer[object]:
    """Build an inline serializer for the DRF paginated envelope.

    Document the real wire shape of ``LimitOffsetPagination`` responses —
    a bare ``child(many=True)`` hides ``count``/``next``/``previous``.
    """
    return inline_serializer(
        name,
        {
            "count": drf_serializers.IntegerField(),
            "next": drf_serializers.URLField(allow_null=True),
            "previous": drf_serializers.URLField(allow_null=True),
            "results": child,
        },
    )


class AuthScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "auth"


class AuthPublicView(AuthScopedView):
    """Auth-scope view that bypasses authentication (register, login, OAuth, …)."""

    permission_classes: ClassVar[list[type[BasePermission]]] = [AllowAny]  # type: ignore[misc]


class AuthLoginView(AuthPublicView):
    """Login endpoint — narrower scope than `auth` so a refresh burst can't starve logins."""

    throttle_scope = "auth_login"


class AuthRegisterView(AuthPublicView):
    """Register endpoint — own scope so signup bursts are isolated from login/refresh."""

    throttle_scope = "auth_register"


class AuthRefreshView(AuthPublicView):
    """Refresh-token endpoint — higher per-minute cap for SPA reconnect bursts."""

    throttle_scope = "auth_refresh"


class BillingScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "billing"


class OrgsScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"


class AccountScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "account"
