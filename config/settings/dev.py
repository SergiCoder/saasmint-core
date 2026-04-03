"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True
# Wildcard: treat every IP as internal so django-debug-toolbar works
# regardless of whether the request comes from localhost or a Docker network.
INTERNAL_IPS = type("WildcardIPs", (), {"__contains__": lambda self, addr: True})()
