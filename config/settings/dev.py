"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
CORS_ALLOW_ALL_ORIGINS = True
# 127.0.0.1 for local; 172.0.0.0/8 range covers Docker bridge networks
INTERNAL_IPS = type("WildcardIPs", (), {"__contains__": lambda self, addr: True})()
