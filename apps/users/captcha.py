"""reCAPTCHA v3 token verification for the abuse-prone auth endpoints.

The frontend can only *acquire* a v3 token (public site key, no enforcement
power); this module *verifies* it server-side, because an attacker can call the
API directly and never touch the SPA. Verification is a no-op when
``RECAPTCHA_SECRET_KEY`` is unset, so local dev and the test suite run keyless.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)

_SITEVERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
_TIMEOUT = httpx.Timeout(5.0)


class CaptchaFailedError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "CAPTCHA verification failed. Please try again."
    default_code = "captcha_failed"


def verify_recaptcha(token: str, *, action: str, remote_ip: str | None = None) -> None:
    """Verify a reCAPTCHA v3 token against Google's ``siteverify`` endpoint.

    No-op when ``RECAPTCHA_SECRET_KEY`` is unset (feature disabled). Raises
    ``CaptchaFailedError`` (HTTP 400, ``code="captcha_failed"``) when the token
    is missing, unsuccessful, issued for the wrong ``action``, or scores below
    ``RECAPTCHA_MIN_SCORE``.

    **Fails open:** if Google is unreachable — a timeout, a 5xx (surfaced via
    ``raise_for_status``), or an unparseable body — the request is allowed and a
    warning is logged, so a Google outage can't lock everyone out. Existing
    throttles still apply.
    """
    if not settings.RECAPTCHA_SECRET_KEY:
        return
    if not token:
        raise CaptchaFailedError

    payload: dict[str, str] = {
        "secret": settings.RECAPTCHA_SECRET_KEY,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        resp = httpx.post(_SITEVERIFY_URL, data=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        # Network error, 5xx, or non-JSON body — fail open (see docstring).
        logger.warning(
            "reCAPTCHA siteverify unreachable; allowing request (fail-open)",
            exc_info=True,
        )
        return

    if (
        not data.get("success", False)
        or data.get("action") != action
        or data.get("score", 0.0) < settings.RECAPTCHA_MIN_SCORE
    ):
        logger.info(
            "reCAPTCHA rejected: success=%s action=%s (want %s) score=%s",
            data.get("success"),
            data.get("action"),
            action,
            data.get("score"),
        )
        raise CaptchaFailedError
