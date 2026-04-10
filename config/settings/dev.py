"""Development settings — debug on, CORS open, relaxed security."""

from config.settings.base import *  # noqa: F403  # star import intentional for settings inheritance pattern

DEBUG = True
# Accept any Host header in dev so requests from inside the docker network
# (e.g. stripe-cli forwarding to http://django:8001/...) aren't rejected.
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True
# Trust the X-Forwarded-Proto header from Caddy so build_absolute_uri()
# generates https:// URLs behind the TLS proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
# Wildcard: treat every IP as internal so django-debug-toolbar works
# regardless of whether the request comes from localhost or a Docker network.
INTERNAL_IPS = type("WildcardIPs", (), {"__contains__": lambda self, addr: True})()
