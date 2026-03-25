"""Base Django settings shared across all environments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: base.py → settings/ → config/ → repo
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Select the active env file based on ENVIRONMENT (default: local)
_ENV_NAME = os.environ.get("ENVIRONMENT", "local")
_ENV_FILE_MAP = {
    "local": ".env.local",
    "development": ".env.dev",
    "production": ".env.prod",
}
_ACTIVE_ENV = _REPO_ROOT / _ENV_FILE_MAP.get(_ENV_NAME, ".env.local")


class _Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(_ACTIVE_ENV),
            str(_REPO_ROOT / ".env.django"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    django_secret_key: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_secret: str
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://localhost:5432/stripe_saas"
    debug: bool = False
    allowed_hosts: list[str] = []
    cors_allowed_origins: list[str] = []
    cors_allow_all_origins: bool = False
    enable_session_auth: bool = False  # dev-only: allows browsable API via Django session


env = _Env()  # type: ignore[call-arg]


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
DEBUG = env.debug
ALLOWED_HOSTS = env.allowed_hosts

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "hijack",
    "hijack.contrib.admin",
    "apps.users",
    "apps.billing",
    "apps.orgs",
    "apps.admin_panel",
    "apps.dashboard",
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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": _parse_db_url(env.database_url)}

AUTH_USER_MODEL = "users.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

_auth_classes = ["apps.users.authentication.SupabaseJWTAuthentication"]
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
        "auth": "10/minute",
        "billing": "30/hour",
        "account": "10/hour",
        "account_export": "3/hour",
        "orgs": "60/hour",
    },
    "EXCEPTION_HANDLER": "middleware.exceptions.domain_exception_handler",
}

CORS_ALLOWED_ORIGINS = env.cors_allowed_origins
CORS_ALLOW_ALL_ORIGINS = env.cors_allow_all_origins

# Celery
CELERY_BROKER_URL = env.redis_url
CELERY_RESULT_BACKEND = env.redis_url
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

# Stripe
STRIPE_SECRET_KEY = env.stripe_secret_key
STRIPE_WEBHOOK_SECRET = env.stripe_webhook_secret

# Supabase
SUPABASE_URL = env.supabase_url
SUPABASE_ANON_KEY = env.supabase_anon_key
SUPABASE_JWT_SECRET = env.supabase_jwt_secret

# django-hijack
HIJACK_REGISTER_ADMIN_ACTIONS = True
HIJACK_PERMISSION_CHECK = "hijack.permissions.superusers_only"
HIJACK_LOGIN_REDIRECT_URL = "/dashboard/"
