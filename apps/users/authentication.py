"""JWT authentication backend for Django REST Framework.

Django issues and verifies its own HS256 JWTs — no external auth provider.
Refresh tokens are stored in the database for revocation and rotation.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from django.conf import settings
from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from apps.users.models import AUTH_USER_CACHE_KEY, RefreshToken, User

logger = logging.getLogger(__name__)

_AUTH_CACHE_TTL = 60  # seconds

# Token lifetimes
ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
EMAIL_VERIFICATION_LIFETIME = timedelta(hours=24)
PASSWORD_RESET_LIFETIME = timedelta(hours=1)

_ALGORITHM = "HS256"


def _get_signing_key() -> str:
    return settings.JWT_SIGNING_KEY


def _hash_token(raw: str) -> str:
    """SHA-256 hash for storing tokens server-side."""
    return hashlib.sha256(raw.encode()).hexdigest()


def create_access_token(user: User) -> str:
    """Issue a short-lived access token for the given user."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "type": "access",
        "iat": now,
        "exp": now + ACCESS_TOKEN_LIFETIME,
    }
    return jwt.encode(payload, _get_signing_key(), algorithm=_ALGORITHM)


def create_refresh_token(user: User) -> str:
    """Issue a DB-backed refresh token. Returns the raw opaque token."""
    raw = secrets.token_urlsafe(48)
    RefreshToken.objects.create(
        user=user,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(UTC) + REFRESH_TOKEN_LIFETIME,
    )
    return raw


def rotate_refresh_token(raw_token: str) -> tuple[User, str]:
    """Validate, revoke, and reissue a refresh token. Returns (user, new_raw_token).

    Raises AuthenticationFailed on invalid/expired/revoked tokens.
    """
    token_hash = _hash_token(raw_token)
    try:
        rt = RefreshToken.objects.select_related("user").get(token_hash=token_hash)
    except RefreshToken.DoesNotExist:
        raise AuthenticationFailed(
            {"detail": "Invalid refresh token.", "code": "invalid_token"}
        ) from None

    if rt.revoked_at is not None:
        # Possible token reuse — revoke all tokens for this user as a precaution
        RefreshToken.objects.filter(user=rt.user, revoked_at__isnull=True).update(
            revoked_at=datetime.now(UTC)
        )
        raise AuthenticationFailed({"detail": "Token has been revoked.", "code": "token_revoked"})

    if rt.expires_at <= datetime.now(UTC):
        raise AuthenticationFailed(
            {"detail": "Refresh token has expired.", "code": "token_expired"}
        )

    user = rt.user
    if not user.is_active:
        raise AuthenticationFailed({"detail": "User not found.", "code": "user_not_found"})

    # Revoke old, issue new
    rt.revoked_at = datetime.now(UTC)
    rt.save(update_fields=["revoked_at"])

    new_raw = create_refresh_token(user)
    return user, new_raw


def revoke_refresh_token(raw_token: str) -> None:
    """Revoke a single refresh token (logout)."""
    token_hash = _hash_token(raw_token)
    RefreshToken.objects.filter(token_hash=token_hash, revoked_at__isnull=True).update(
        revoked_at=datetime.now(UTC)
    )


def revoke_all_refresh_tokens(user: User) -> None:
    """Revoke all refresh tokens for a user (e.g. password change)."""
    RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=datetime.now(UTC)
    )


def _create_one_time_token(
    model_class: Any,  # noqa: ANN401
    user: User,
    lifetime: timedelta,
) -> str:
    """Create a hashed one-time token for *model_class*. Returns the raw token."""
    raw = secrets.token_urlsafe(32)
    model_class.objects.create(
        user=user,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(UTC) + lifetime,
    )
    return raw


def _verify_one_time_token(model_class: Any, raw_token: str, label: str) -> User:  # noqa: ANN401
    """Validate and consume a one-time token. Returns the user.

    Raises AuthenticationFailed on invalid/expired/used tokens.
    """
    token_hash = _hash_token(raw_token)
    try:
        obj = model_class.objects.select_related("user").get(token_hash=token_hash)
    except model_class.DoesNotExist:
        raise AuthenticationFailed(
            {"detail": f"Invalid {label} token.", "code": "invalid_token"}
        ) from None

    if obj.used_at is not None:
        raise AuthenticationFailed({"detail": "Token has already been used.", "code": "token_used"})
    if obj.expires_at <= datetime.now(UTC):
        raise AuthenticationFailed({"detail": "Token has expired.", "code": "token_expired"})

    user: User = obj.user
    if not user.is_active:
        raise AuthenticationFailed({"detail": "User not found.", "code": "user_not_found"})

    obj.used_at = datetime.now(UTC)
    obj.save(update_fields=["used_at"])
    return user


def create_email_verification_token(user: User) -> str:
    """Create a one-time email verification token. Returns the raw token."""
    from apps.users.models import EmailVerificationToken

    return _create_one_time_token(EmailVerificationToken, user, EMAIL_VERIFICATION_LIFETIME)


def verify_email_token(raw_token: str) -> User:
    """Validate and consume an email verification token. Returns the user."""
    from apps.users.models import EmailVerificationToken

    return _verify_one_time_token(EmailVerificationToken, raw_token, "verification")


def create_password_reset_token(user: User) -> str:
    """Create a one-time password reset token. Returns the raw token."""
    from apps.users.models import PasswordResetToken

    return _create_one_time_token(PasswordResetToken, user, PASSWORD_RESET_LIFETIME)


def verify_password_reset_token(raw_token: str) -> User:
    """Validate and consume a password reset token. Returns the user."""
    from apps.users.models import PasswordResetToken

    return _verify_one_time_token(PasswordResetToken, raw_token, "reset")


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
                user = User.objects.get(id=user_id, is_active=True)
            except User.DoesNotExist:
                raise AuthenticationFailed(
                    {"detail": "User not found.", "code": "user_not_found"}
                ) from None
            cache.set(cache_key, user, timeout=_AUTH_CACHE_TTL)

        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"
