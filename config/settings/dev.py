"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
# Enumerate dev hosts explicitly. With USE_X_FORWARDED_HOST=True, a wildcard
# here lets a forged X-Forwarded-Host poison request.build_absolute_uri() and
# thus OAuth redirect URIs. Extend via ALLOWED_HOSTS env var for custom setups.
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "django",  # docker service name (stripe-cli forwards here)
    "dev.saasmint.net",
    *ALLOWED_HOSTS,  # noqa: F405  # from env via base.py star import
]
CORS_ALLOW_ALL_ORIGINS = True
# Caddy terminates TLS and forwards X-Forwarded-Proto: https — trust it so
# request.build_absolute_uri() produces https:// URLs (needed for OAuth redirects).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
