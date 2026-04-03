"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True
INTERNAL_IPS = ["127.0.0.1"]
