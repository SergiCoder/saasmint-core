"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True
