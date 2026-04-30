"""Base Django settings shared across all environments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: base.py → settings/ → config/ → repo
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Select the active env file based on ENVIRONMENT (default: local).
# Accepted values: local | dev | prod. Anything else falls back to .env.local.
_ENV_NAME = os.environ.get("ENVIRONMENT", "local")
_ENV_FILE_MAP = {
    "local": ".env.local",
    "dev": ".env.dev",
    "prod": ".env.prod",
}
_ACTIVE_ENV = _REPO_ROOT / _ENV_FILE_MAP.get(_ENV_NAME, ".env.local")


class _Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_ACTIVE_ENV),),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    django_secret_key: str
    jwt_signing_key: str = ""  # if empty, falls back to django_secret_key
    stripe_secret_key: str
    stripe_webhook_secret: str
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://localhost:5432/saasmint"
    environment: str = "local"  # local | dev | prod — surfaced in admin banner + logs
    schema_public: bool = False  # expose /api/schema, /api/docs, /api/redoc outside DEBUG
    allowed_hosts: list[str] = []
    cors_allowed_origins: list[str] = []
    cors_allow_all_origins: bool = False
    csrf_trusted_origins: list[str] = []
    resend_api_key: str = ""
    frontend_url: str = "https://localhost:3000"
    email_from_address: str = "noreply@saasmint.net"
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_microsoft_client_id: str = ""
    oauth_microsoft_client_secret: str = ""
    marketing_inquiries_to: str = ""  # admin inbox for landing-CTA / Contact-form submissions
    enable_session_auth: bool = False  # dev-only: allows browsable API via Django session


env = _Env()  # type: ignore[call-arg]  # pydantic-settings reads fields from env vars at construction; mypy sees no positional args but none are needed


def _parse_db_url(url: str) -> dict[str, object]:
    # Strip SQLAlchemy driver suffixes (+asyncpg, +psycopg) so the URL parses
    # correctly when DATABASE_URL is shared with SQLAlchemy-based backends.
    has_driver = "+" in url.split("://")[0]
    clean = url.split("+")[0] + "://" + url.split("://", 1)[1] if has_driver else url
    parsed = urlparse(clean)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "localhost",
        "PORT": str(parsed.port or 5432),
    }


BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = env.django_secret_key
JWT_SIGNING_KEY = env.jwt_signing_key or env.django_secret_key
ENVIRONMENT = env.environment
SCHEMA_PUBLIC = env.schema_public
ALLOWED_HOSTS = env.allowed_hosts

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "hijack",
    "hijack.contrib.admin",
    "apps.users",
    "apps.billing",
    "apps.orgs",
    "apps.admin_panel",
    "apps.dashboard",
    "apps.marketing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "middleware.security.SecurityHeadersMiddleware",
    "hijack.middleware.HijackUserMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": _parse_db_url(env.database_url)}

AUTH_USER_MODEL = "users.User"
LOGIN_URL = "/admin/login/"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

_auth_classes = ["apps.users.authentication.JWTAuthentication"]
if env.enable_session_auth:
    _auth_classes.append("rest_framework.authentication.SessionAuthentication")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": _auth_classes,
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
        # `auth` is the shared bucket for verify-email, reset, change-password,
        # OAuth start/callback, and invitation accept/decline — all low-volume
        # but bursty flows. Login, refresh, and register are split into their
        # own scopes so SPA reconnect bursts on refresh do not starve login
        # attempts, and vice versa.
        "auth": "10/minute",
        "auth_login": "5/minute",
        "auth_register": "5/minute",
        "auth_refresh": "60/minute",
        # Marketing inquiries get a dedicated, much tighter scope: the failure
        # mode (admin inbox flooded by a single IP) is more direct than auth's
        # (outbound spam, contained by Resend reputation), and the traffic
        # shape is one-and-done rather than bursty.
        "marketing_inquiries": "3/10minute",
        "billing": "100/hour",
        "account": "120/hour",
        "account_export": "3/hour",
        "orgs": "60/hour",
        "references": "120/hour",
    },
    "EXCEPTION_HANDLER": "middleware.exceptions.domain_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SaasMint Core API",
    "DESCRIPTION": "Django backend API for SaasMint — billing, accounts, and organizations.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]",
    "COMPONENT_SPLIT_REQUEST": True,
    "PREPROCESSING_HOOKS": [
        "config.spectacular_hooks.preprocess_exclude_spectacular_views",
    ],
    "EXCLUDE_PATH_REGEX": [
        r"^/admin/",
        r"^/hijack/",
        r"^/dashboard/",
        r"^/api/v1/webhooks/",
    ],
}

CORS_ALLOWED_ORIGINS = env.cors_allowed_origins
CORS_ALLOW_ALL_ORIGINS = env.cors_allow_all_origins
CSRF_TRUSTED_ORIGINS = env.csrf_trusted_origins

# Cache — shared Redis across all workers. LocMemCache would shard per
# process and break OAuth one-time-code exchange, per-user auth-cache
# invalidation, and exchange-rate fan-out under multi-worker load.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.redis_url,
    },
}

# Celery
CELERY_BROKER_URL = env.redis_url
CELERY_RESULT_BACKEND = env.redis_url
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE = {
    "sync-exchange-rates": {
        "task": "apps.billing.tasks.sync_exchange_rates",
        "schedule": 86400,  # once per day
    },
    "cleanup-expired-refresh-tokens": {
        "task": "apps.users.tasks.cleanup_expired_refresh_tokens",
        "schedule": 86400,  # once per day
    },
}

# Stripe
STRIPE_SECRET_KEY = env.stripe_secret_key
STRIPE_WEBHOOK_SECRET = env.stripe_webhook_secret

# Email (Resend)
RESEND_API_KEY = env.resend_api_key
EMAIL_FROM_ADDRESS = env.email_from_address
FRONTEND_URL = env.frontend_url

# OAuth
OAUTH_GOOGLE_CLIENT_ID = env.oauth_google_client_id
OAUTH_GOOGLE_CLIENT_SECRET = env.oauth_google_client_secret
OAUTH_GITHUB_CLIENT_ID = env.oauth_github_client_id
OAUTH_GITHUB_CLIENT_SECRET = env.oauth_github_client_secret
OAUTH_MICROSOFT_CLIENT_ID = env.oauth_microsoft_client_id
OAUTH_MICROSOFT_CLIENT_SECRET = env.oauth_microsoft_client_secret

# Marketing
MARKETING_INQUIRIES_TO = env.marketing_inquiries_to

# django-hijack
HIJACK_REGISTER_ADMIN_ACTIONS = True
HIJACK_PERMISSION_CHECK = "hijack.permissions.superusers_only"
HIJACK_LOGIN_REDIRECT_URL = "/dashboard/"
