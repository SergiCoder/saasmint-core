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
            else:
                # Every other HTML surface — Django admin, hijack acquire/
                # release, the /dashboard/ landing page used by hijack, and
                # DRF's browsable API at /api/... — relies on inline styles
                # and self scripts. JSON API responses never reach this branch
                # (the outer content-type check excludes them), so widening
                # the policy to all HTML is safe: the strict ``default-src
                # 'none'`` fallback was only ever blocking dev tooling.
                # Explicit ``frame-ancestors 'self'`` blocks third-party
                # clickjacking embeds.
                response["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; frame-ancestors 'self'"
                )
        return response
