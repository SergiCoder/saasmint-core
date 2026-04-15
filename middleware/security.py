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
            path = request.path
            if path.startswith(("/api/docs/", "/api/redoc/")):
                # Swagger UI and ReDoc load assets from external CDNs
                cdn = "https://cdn.jsdelivr.net"
                fonts = "https://fonts.googleapis.com https://fonts.gstatic.com"
                redoc = "https://cdn.redoc.ly"
                response["Content-Security-Policy"] = (
                    f"default-src 'self'; "
                    f"script-src 'self' 'unsafe-inline' {cdn}; "
                    f"style-src 'self' 'unsafe-inline' {cdn} {fonts}; "
                    f"font-src 'self' {fonts}; "
                    f"img-src 'self' data: {cdn} {redoc}; "
                    f"worker-src blob:; "
                    f"connect-src 'self' {cdn}"
                )
            elif path.startswith("/admin/") or path.startswith("/hijack/"):
                # Django admin and DRF browsable API rely on inline styles.
                response["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
                )
            else:
                # API responses don't render inline styles or scripts; lock down CSP.
                response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response
