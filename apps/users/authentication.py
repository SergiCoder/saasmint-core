"""JWT authentication backend for Django REST Framework.

Django issues and verifies its own HS256 JWTs — no external auth provider.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import jwt
from django.conf import settings
from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from apps.users.models import AUTH_USER_CACHE_KEY, User

logger = logging.getLogger(__name__)

_AUTH_CACHE_TTL = 60  # seconds

# Token lifetimes (seconds)
ACCESS_TOKEN_LIFETIME = 60 * 15  # 15 minutes
REFRESH_TOKEN_LIFETIME = 60 * 60 * 24 * 7  # 7 days

_ALGORITHM = "HS256"


def _get_signing_key() -> str:
    return settings.SECRET_KEY


def create_access_token(user: User) -> str:
    """Issue a short-lived access token for the given user."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "type": "access",
        "iat": now,
        "exp": now.timestamp() + ACCESS_TOKEN_LIFETIME,
    }
    return jwt.encode(payload, _get_signing_key(), algorithm=_ALGORITHM)


def create_refresh_token(user: User) -> str:
    """Issue a long-lived refresh token for the given user."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "type": "refresh",
        "iat": now,
        "exp": now.timestamp() + REFRESH_TOKEN_LIFETIME,
    }
    return jwt.encode(payload, _get_signing_key(), algorithm=_ALGORITHM)


class JWTAuthentication(BaseAuthentication):
    """Authenticate requests using a Django-issued JWT Bearer token."""

    def authenticate(self, request: Request) -> tuple[User, str] | None:
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1]

        try:
            payload: dict[str, object] = jwt.decode(
                token,
                _get_signing_key(),
                algorithms=[_ALGORITHM],
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed(
                {"detail": "Token has expired.", "code": "token_expired"}
            ) from exc
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT verification failed")
            raise AuthenticationFailed(
                {"detail": "Invalid token.", "code": "invalid_token"}
            ) from exc

        # Only accept access tokens for API authentication
        if payload.get("type") != "access":
            raise AuthenticationFailed({"detail": "Invalid token type.", "code": "invalid_token"})

        user_id = str(payload.get("sub", ""))
        if not user_id:
            raise AuthenticationFailed(
                {"detail": "Token missing 'sub' claim.", "code": "invalid_token"}
            )

        cache_key = AUTH_USER_CACHE_KEY.format(user_id)
        user: User | None = cache.get(cache_key)
        if user is None:
            try:
                user = User.objects.get(id=user_id, deleted_at__isnull=True, is_active=True)
            except User.DoesNotExist:
                raise AuthenticationFailed(
                    {"detail": "User not found.", "code": "user_not_found"}
                ) from None
            cache.set(cache_key, user, timeout=_AUTH_CACHE_TTL)

        # Safety net: reject users whose scheduled deletion date has passed
        if user.scheduled_deletion_at is not None and user.scheduled_deletion_at <= datetime.now(
            UTC
        ):
            cache.delete(cache_key)
            raise AuthenticationFailed(
                {"detail": "Account has been deleted.", "code": "account_deleted"}
            )

        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"
