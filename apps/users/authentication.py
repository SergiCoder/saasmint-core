"""Supabase JWT authentication backend for Django REST Framework."""

from __future__ import annotations

import jwt
from django.conf import settings
from django.core.cache import cache
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from apps.users.models import User

_AUTH_CACHE_TTL = 60  # seconds


class SupabaseJWTAuthentication(BaseAuthentication):
    """Authenticate requests using a Supabase-issued JWT Bearer token."""

    def authenticate(self, request: Request) -> tuple[User, str] | None:
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1]

        try:
            payload: dict[str, object] = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed("Token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationFailed("Invalid token.") from exc

        supabase_uid = str(payload.get("sub", ""))
        if not supabase_uid:
            raise AuthenticationFailed("Token missing 'sub' claim.")

        if not payload.get("email_verified", False):
            raise AuthenticationFailed("Email not verified.") from None

        cache_key = f"auth_user:{supabase_uid}"
        user: User | None = cache.get(cache_key)
        if user is None:
            try:
                user = User.objects.only(
                    "id",
                    "email",
                    "supabase_uid",
                    "full_name",
                    "preferred_locale",
                    "account_type",
                    "is_active",
                    "is_staff",
                    "is_verified",
                ).get(supabase_uid=supabase_uid, deleted_at__isnull=True, is_active=True)
            except User.DoesNotExist:
                email = str(payload.get("email", ""))
                if not email:
                    raise AuthenticationFailed("Token missing 'email' claim.") from None
                # Prevent resurrecting soft-deleted or deactivated users
                if User.objects.filter(supabase_uid=supabase_uid).exists():
                    raise AuthenticationFailed("Account is deactivated.") from None
                user, _ = User.objects.get_or_create(
                    supabase_uid=supabase_uid,
                    defaults={"email": email, "is_verified": True},
                )
            cache.set(cache_key, user, timeout=_AUTH_CACHE_TTL)

        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"
