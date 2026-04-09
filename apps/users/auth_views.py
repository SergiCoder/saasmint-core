"""Authentication API views — register, login, refresh, logout."""

from __future__ import annotations

import logging
from typing import ClassVar

from django.contrib.auth import authenticate
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.billing.services import assign_free_plan
from apps.users.auth_serializers import (
    LoginSerializer,
    RefreshSerializer,
    RegisterSerializer,
    TokenResponseSerializer,
)
from apps.users.authentication import (
    _ALGORITHM,
    _get_signing_key,
    create_access_token,
    create_refresh_token,
)
from apps.users.models import User

logger = logging.getLogger(__name__)


class RegisterView(APIView):
    """POST /api/v1/auth/register — create a new account."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(request=RegisterSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        email = ser.validated_data["email"]
        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "Email already registered.", "code": "email_exists"},
                status=status.HTTP_409_CONFLICT,
            )

        user = User.objects.create_user(
            email=email,
            password=ser.validated_data["password"],
            full_name=ser.validated_data["full_name"],
            is_verified=True,
        )
        assign_free_plan(user)

        return Response(
            {
                "access_token": create_access_token(user),
                "refresh_token": create_refresh_token(user),
                "token_type": "Bearer",
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """POST /api/v1/auth/login — authenticate with email + password."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(request=LoginSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=ser.validated_data["email"],
            password=ser.validated_data["password"],
        )
        if user is None or not isinstance(user, User):
            return Response(
                {"detail": "Invalid credentials.", "code": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account is deactivated.", "code": "account_deactivated"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {
                "access_token": create_access_token(user),
                "refresh_token": create_refresh_token(user),
                "token_type": "Bearer",
            },
        )


class RefreshView(APIView):
    """POST /api/v1/auth/refresh — exchange a refresh token for new tokens."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(request=RefreshSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        import jwt

        ser = RefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            payload = jwt.decode(
                ser.validated_data["refresh_token"],
                _get_signing_key(),
                algorithms=[_ALGORITHM],
            )
        except jwt.InvalidTokenError:
            return Response(
                {"detail": "Invalid refresh token.", "code": "invalid_token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if payload.get("type") != "refresh":
            return Response(
                {"detail": "Invalid token type.", "code": "invalid_token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            user = User.objects.get(id=payload["sub"], is_active=True, deleted_at__isnull=True)
        except User.DoesNotExist:
            return Response(
                {"detail": "User not found.", "code": "user_not_found"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {
                "access_token": create_access_token(user),
                "refresh_token": create_refresh_token(user),
                "token_type": "Bearer",
            },
        )
