"""Test settings — provides dummy secrets so _Env validation passes."""

import os

# Provide dummy values for required secrets that aren't in .env.base.
# These are set BEFORE base.py is imported so _Env() picks them up.
_TEST_DEFAULTS = {
    "STRIPE_SECRET_KEY": "sk_test_fake",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_fake",
    "DJANGO_SECRET_KEY": "django-insecure-test-key",
}

for key, value in _TEST_DEFAULTS.items():
    os.environ.setdefault(key, value)

from config.settings.base import *  # noqa: F403, E402  # star import intentional for settings inheritance; E402 because env vars must be set before import

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOWED_ORIGINS = [
    "https://example.com",
]

# Tests run without Redis — fall back to in-process cache. Each test runs
# in a single process so LocMemCache's per-process isolation is harmless.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}
