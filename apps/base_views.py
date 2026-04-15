"""Shared DRF base views — permission and throttle scaffolding reused across apps."""

from __future__ import annotations

from typing import ClassVar

from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView


class AuthScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "auth"


class AuthPublicView(AuthScopedView):
    """Auth-scope view that bypasses authentication (register, login, OAuth, …)."""

    permission_classes: ClassVar[list[type[BasePermission]]] = [AllowAny]  # type: ignore[misc]


class BillingScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "billing"


class OrgsScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"


class AccountScopedView(APIView):
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "account"
