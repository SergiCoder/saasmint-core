"""Production settings — debug off, strict security headers."""

import os
import re

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = False

# JWT_SIGNING_KEY MUST be configured independently from SECRET_KEY in
# production: rotating one shouldn't force the other to rotate too. The
# base settings fall back to ``DJANGO_SECRET_KEY`` when unset, but that
# coupling is unsafe in prod — it means any audit/rotation of the Django
# secret silently invalidates every issued JWT. Require the explicit env
# var here so a missing JWT_SIGNING_KEY in prod fails loudly at boot
# instead of silently taking the SECRET_KEY shortcut.
_jwt_signing_key = os.environ.get("JWT_SIGNING_KEY", "")
if not _jwt_signing_key:
    raise ImproperlyConfigured(
        "JWT_SIGNING_KEY must be set in production (independent rotation"
        " lifecycle from SECRET_KEY)."
    )
# HS256 signs every access/refresh JWT with this key. A key shorter than the
# 32-byte SHA-256 output weakens the signature (RFC 7518 §3.2) and makes PyJWT
# (>=2.13) emit InsecureKeyLengthWarning at runtime. Fail loudly at boot rather
# than ship a weak key — generate one with `secrets.token_urlsafe(48)`.
if len(_jwt_signing_key.encode()) < 32:
    raise ImproperlyConfigured(
        "JWT_SIGNING_KEY must be at least 32 bytes for HS256 (RFC 7518 §3.2). "
        'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

# Session-based DRF auth is a dev-only convenience for the browsable API;
# enabling it in production exposes mutating endpoints to CSRF + cookie-based
# attacks even when the JWT path is the documented entry point. The "dev-only"
# label on the env var is too easy to miss — fail boot if it's set.
if os.environ.get("ENABLE_SESSION_AUTH", "").lower() in ("1", "true", "yes"):
    raise ImproperlyConfigured(
        "ENABLE_SESSION_AUTH must not be set in production — it adds DRF "
        "SessionAuthentication, which weakens the JWT-only auth surface."
    )

_HOST_RE = re.compile(r"^(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*$")

if not ALLOWED_HOSTS:  # noqa: F405  # ALLOWED_HOSTS imported via star import above; F405 expected
    raise ImproperlyConfigured("ALLOWED_HOSTS must be explicitly set in production.")
for _host in ALLOWED_HOSTS:  # noqa: F405
    # Reject wildcards (including leading-dot subdomain wildcards like ".example.com")
    # and anything that isn't a plain hostname. Prevents Host-header attacks.
    if "*" in _host or not _HOST_RE.match(_host.lstrip(".")):
        raise ImproperlyConfigured(
            f"ALLOWED_HOSTS entry {_host!r} is not a valid hostname (no wildcards allowed)."
        )

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CORS_ALLOW_ALL_ORIGINS = False

if not CSRF_TRUSTED_ORIGINS:  # noqa: F405
    # Secure cookies + empty CSRF_TRUSTED_ORIGINS locks out browser-based
    # cross-origin POSTs from the frontend. Require explicit configuration.
    raise ImproperlyConfigured(
        "CSRF_TRUSTED_ORIGINS must be set in production (required with SESSION_COOKIE_SECURE=True)."
    )
