"""Read-only reference-data endpoints for user profile dropdowns."""

from __future__ import annotations

from typing import ClassVar
from zoneinfo import available_timezones

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from saasmint_core.services.currency import SUPPORTED_CURRENCIES
from saasmint_core.services.locale import SUPPORTED_LOCALES
from saasmint_core.services.phone import SUPPORTED_PHONE_PREFIXES


class LocaleListView(APIView):
    """GET /api/v1/locales/ — list supported locales."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "references"

    @extend_schema(responses={200: list[str]}, tags=["references"])
    def get(self, request: Request) -> Response:
        return Response(sorted(SUPPORTED_LOCALES))


class CurrencyListView(APIView):
    """GET /api/v1/currencies/ — list supported currencies."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "references"

    @extend_schema(responses={200: list[str]}, tags=["references"])
    def get(self, request: Request) -> Response:
        return Response(sorted(SUPPORTED_CURRENCIES))


_PHONE_PREFIXES: list[dict[str, str]] = [
    {"prefix": k, "label": v}
    for k, v in sorted(SUPPORTED_PHONE_PREFIXES.items(), key=lambda x: int(x[0].lstrip("+")))
]


class PhonePrefixListView(APIView):
    """GET /api/v1/phone-prefixes/ — list supported phone prefixes."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "references"

    @extend_schema(responses={200: list[dict[str, str]]}, tags=["references"])
    def get(self, request: Request) -> Response:
        return Response(_PHONE_PREFIXES)


_TIMEZONES: list[str] = sorted(available_timezones())


class TimezoneListView(APIView):
    """GET /api/v1/timezones/ — list IANA timezones."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "references"

    @extend_schema(responses={200: list[str]}, tags=["references"])
    def get(self, request: Request) -> Response:
        return Response(_TIMEZONES)
