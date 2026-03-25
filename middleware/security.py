"""Security headers middleware — applied to every response."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse


class SecurityHeadersMiddleware:
    """Add standard security headers to every response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response["X-Content-Type-Options"] = "nosniff"
        # X-Frame-Options handled by Django's XFrameOptionsMiddleware — not duplicated here
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # X-XSS-Protection intentionally omitted — deprecated and can cause vulnerabilities
        if "text/html" in response.get("Content-Type", ""):
            response["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
            )
        return response
