"""Tests for SecurityHeadersMiddleware — path-prefix CSP opt-in."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from middleware.security import SecurityHeadersMiddleware


def _html_response(_req: HttpRequest) -> HttpResponse:
    return HttpResponse("<html></html>", content_type="text/html; charset=utf-8")


def _json_response(_req: HttpRequest) -> HttpResponse:
    return HttpResponse('{"ok": true}', content_type="application/json")


class TestAlwaysOnHeaders:
    def test_sets_x_content_type_options(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/v1/health/"))
        assert resp["X-Content-Type-Options"] == "nosniff"

    def test_sets_referrer_policy(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/v1/health/"))
        assert resp["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_sets_permissions_policy(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/v1/health/"))
        assert "camera=()" in resp["Permissions-Policy"]
        assert "microphone=()" in resp["Permissions-Policy"]
        assert "geolocation=()" in resp["Permissions-Policy"]

    def test_does_not_set_x_xss_protection(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/v1/health/"))
        assert "X-XSS-Protection" not in resp


class TestCSPOptIn:
    def test_no_csp_for_non_html_responses(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/v1/health/"))
        assert "Content-Security-Policy" not in resp

    def test_api_html_gets_strict_csp(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/api/v1/users/"))
        assert resp["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_docs_path_gets_loose_csp_with_cdn(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/api/docs/"))
        csp = resp["Content-Security-Policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "https://fonts.googleapis.com" in csp
        assert "https://cdn.redoc.ly" in csp
        assert "default-src 'self'" in csp
        assert "'unsafe-inline'" in csp

    def test_redoc_path_gets_loose_csp_with_cdn(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/api/redoc/"))
        csp = resp["Content-Security-Policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "https://cdn.redoc.ly" in csp

    def test_admin_path_gets_moderate_csp_with_frame_ancestors_self(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/admin/login/"))
        csp = resp["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "default-src 'self'" in csp

    def test_hijack_path_gets_moderate_csp(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/hijack/release/"))
        csp = resp["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in csp
        assert "default-src 'self'" in csp

    def test_dashboard_path_gets_moderate_csp(self) -> None:
        """Dashboard is the hijack landing page and renders server-side HTML —
        must allow the admin/hijack CSS to load rather than the strict API CSP."""
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/dashboard/"))
        csp = resp["Content-Security-Policy"]
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "frame-ancestors 'self'" in csp
        assert "default-src 'self'" in csp

    def test_admin_csp_is_distinct_from_api_csp(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        admin = mw(factory.get("/admin/"))
        api = mw(factory.get("/api/v1/users/"))
        assert admin["Content-Security-Policy"] != api["Content-Security-Policy"]

    def test_root_path_gets_strict_api_csp(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_html_response)
        resp = mw(factory.get("/"))
        assert resp["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_docs_without_html_content_type_has_no_csp(self) -> None:
        factory = RequestFactory()
        mw = SecurityHeadersMiddleware(_json_response)
        resp = mw(factory.get("/api/docs/"))
        assert "Content-Security-Policy" not in resp
