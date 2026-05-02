"""Development settings — debug on, CORS open, relaxed security."""

from typing import cast

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
# Enumerate dev hosts explicitly. With USE_X_FORWARDED_HOST=True, a wildcard
# here lets a forged X-Forwarded-Host poison request.build_absolute_uri() and
# thus OAuth redirect URIs. Extend via ALLOWED_HOSTS env var for custom setups.
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "django",  # docker service name (stripe-cli forwards here)
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

# Dev static-files strategy: WhiteNoise serves directly from the staticfiles
# finders (i.e. ``STATICFILES_DIRS`` + each app's ``static/`` dir) and re-reads
# the filesystem on every request. That way new or edited assets appear
# without running ``collectstatic`` or rebuilding the container.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

# Dev-only throttle bump: Next.js hot-reload + an attached debugger fan out
# many requests per page render, easily blowing through the prod rates
# within a normal coding session. Lift the scoped buckets here so dev
# doesn't get locked out for the rest of the hour; prod stays tight.
_throttle_rates = cast(
    "dict[str, str]",
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],  # noqa: F405
)
_throttle_rates["user"] = "5000/hour"
_throttle_rates["orgs"] = "5000/hour"
_throttle_rates["billing"] = "5000/hour"
_throttle_rates["account"] = "5000/hour"
_throttle_rates["references"] = "5000/hour"
