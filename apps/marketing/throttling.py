"""Custom rate throttle for the marketing inquiries endpoint.

Marketing inquiries get their own throttle scope so the rate can be tuned
independently of auth flows. The endpoint also wants a tighter burst window
than DRF's standard ``N/minute`` / ``N/hour`` syntax can express
(``3/10minute`` ≈ 18/hr, not 180/hr), so we extend ``parse_rate`` to accept
``N/<count><unit>`` rates while staying backwards compatible with the
standard ``N/<unit>`` form.
"""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView


class MarketingInquiryThrottle(SimpleRateThrottle):
    """Per-IP throttle for ``POST /api/v1/marketing/inquiries/``."""

    scope = "marketing_inquiries"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return f"throttle_{self.scope}_{self.get_ident(request)}"

    def parse_rate(self, rate: str | None) -> tuple[int | None, int | None]:
        if rate is None:
            return (None, None)
        num_str, period = rate.split("/")
        num_requests = int(num_str)
        # Optional integer prefix on the period: "minute", "10minute", "10m".
        i = 0
        while i < len(period) and period[i].isdigit():
            i += 1
        multiplier = int(period[:i]) if i else 1
        unit_char = period[i] if i < len(period) else "s"
        unit_seconds: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return (num_requests, multiplier * unit_seconds[unit_char])

    def __init__(self) -> None:
        # Tolerate a missing rate so tests that swap out REST_FRAMEWORK
        # (clearing this scope from DEFAULT_THROTTLE_RATES) can still
        # instantiate the view. With rate=None, allow_request short-circuits.
        from django.core.exceptions import ImproperlyConfigured

        try:
            super().__init__()
        except ImproperlyConfigured:
            self.rate = None
            self.num_requests = None
            self.duration = None
