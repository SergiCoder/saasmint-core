"""Supabase JWT authentication backend for Django REST Framework."""

from __future__ import annotations

import logging

import jwt
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from apps.users.models import AUTH_USER_CACHE_KEY, User

logger = logging.getLogger(__name__)

_AUTH_CACHE_TTL = 60  # seconds
_JWKS_CACHE_TTL = 3600  # 1 hour
_ALLOWED_ASYMMETRIC_ALGS = {"RS256", "ES256"}

_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    """Return a singleton JWKS client so the key cache persists across requests."""
    global _jwks_client
    if _jwks_client is None:
        jwks_url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        _jwks_client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=_JWKS_CACHE_TTL)
    return _jwks_client


class SupabaseJWTAuthentication(BaseAuthentication):
    """Authenticate requests using a Supabase-issued JWT Bearer token."""

    def authenticate(self, request: Request) -> tuple[User, str] | None:
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1]

        alg = "unknown"
        try:
            # Determine signing algorithm from the token header
            header = jwt.get_unverified_header(token)
            alg = header.get("alg", "HS256")

            if alg in _ALLOWED_ASYMMETRIC_ALGS:
                # Asymmetric algorithm — verify with JWKS public key
                jwks_client = _get_jwks_client()
                signing_key = jwks_client.get_signing_key_from_jwt(token)
                payload: dict[str, object] = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=list(_ALLOWED_ASYMMETRIC_ALGS),
                    audience="authenticated",
                )
            elif alg == "HS256":
                # Symmetric algorithm — verify with shared secret
                payload = jwt.decode(
                    token,
                    settings.SUPABASE_JWT_SECRET,
                    algorithms=["HS256"],
                    audience="authenticated",
                )
            else:
                raise AuthenticationFailed("Unsupported token algorithm.")
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed("Token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT verification failed for alg=%s", alg)
            raise AuthenticationFailed("Invalid token.") from exc
        except (jwt.PyJWKClientError, ConnectionError) as exc:
            logger.error("JWKS fetch error: %s", exc)
            raise AuthenticationFailed("Invalid token.") from exc

        supabase_uid = str(payload.get("sub", ""))
        if not supabase_uid:
            raise AuthenticationFailed("Token missing 'sub' claim.")

        cache_key = AUTH_USER_CACHE_KEY.format(supabase_uid)
        user: User | None = cache.get(cache_key)
        if user is None:
            try:
                user = User.objects.get(
                    supabase_uid=supabase_uid, deleted_at__isnull=True, is_active=True
                )
            except User.DoesNotExist:
                email = str(payload.get("email", ""))
                if not email:
                    raise AuthenticationFailed("Token missing 'email' claim.") from None
                # Prevent resurrecting soft-deleted or deactivated users
                if User.objects.filter(supabase_uid=supabase_uid).exists():
                    raise AuthenticationFailed("Account is deactivated.") from None
                try:
                    user, _ = User.objects.get_or_create(
                        supabase_uid=supabase_uid,
                        deleted_at__isnull=True,
                        defaults={"email": email, "is_verified": True},
                    )
                except IntegrityError:
                    raise AuthenticationFailed(
                        "Email already associated with another account."
                    ) from None
            cache.set(cache_key, user, timeout=_AUTH_CACHE_TTL)

        return (user, token)

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"
