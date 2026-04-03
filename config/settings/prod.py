"""Production settings — debug off, strict security headers."""

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = False

if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:  # noqa: F405  # ALLOWED_HOSTS imported via star import above; F405 expected
    raise ImproperlyConfigured("ALLOWED_HOSTS must be explicitly set in production (no wildcards).")

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CORS_ALLOW_ALL_ORIGINS = False
