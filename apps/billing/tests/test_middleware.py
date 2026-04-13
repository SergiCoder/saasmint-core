"""Tests for middleware — exception handler and security headers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import stripe
from django.test import RequestFactory
from rest_framework.response import Response

from middleware.exceptions import domain_exception_handler
from middleware.security import SecurityHeadersMiddleware


class TestDomainExceptionHandler:
    @pytest.fixture
    def context(self):
        return {"view": MagicMock(), "request": MagicMock()}

    def test_user_not_found_returns_404(self, context):
        from saasmint_core.exceptions import UserNotFoundError

        resp = domain_exception_handler(UserNotFoundError("not found"), context)
        assert resp is not None
        assert resp.status_code == 404

    def test_org_not_found_returns_404(self, context):
        from saasmint_core.exceptions import OrgNotFoundError

        resp = domain_exception_handler(OrgNotFoundError("not found"), context)
        assert resp is not None
        assert resp.status_code == 404

    def test_subscription_not_found_returns_404(self, context):
        from saasmint_core.exceptions import SubscriptionNotFoundError

        resp = domain_exception_handler(SubscriptionNotFoundError("not found"), context)
        assert resp is not None
        assert resp.status_code == 404

    def test_subscription_already_active_returns_409(self, context):
        from saasmint_core.exceptions import SubscriptionAlreadyActiveError

        resp = domain_exception_handler(SubscriptionAlreadyActiveError("active"), context)
        assert resp is not None
        assert resp.status_code == 409

    def test_account_type_conflict_returns_409(self, context):
        from saasmint_core.exceptions import AccountTypeConflictError

        resp = domain_exception_handler(AccountTypeConflictError("conflict"), context)
        assert resp is not None
        assert resp.status_code == 409

    def test_insufficient_permission_returns_403(self, context):
        from saasmint_core.exceptions import InsufficientPermissionError

        resp = domain_exception_handler(InsufficientPermissionError("denied"), context)
        assert resp is not None
        assert resp.status_code == 403

    def test_unknown_domain_error_returns_400(self, context):
        from saasmint_core.exceptions import DomainError

        resp = domain_exception_handler(DomainError("generic"), context)
        assert resp is not None
        assert resp.status_code == 400

    def test_stripe_error_returns_generic_message(self, context):
        exc = stripe.StripeError("internal details")
        exc.http_status = 502
        resp = domain_exception_handler(exc, context)
        assert resp is not None
        assert resp.status_code == 502
        assert "Payment provider error" in resp.data["detail"]
        assert "internal details" not in resp.data["detail"]

    def test_stripe_error_no_status_defaults_to_502(self, context):
        exc = stripe.StripeError("error")
        exc.http_status = None
        resp = domain_exception_handler(exc, context)
        assert resp is not None
        assert resp.status_code == 502

    def test_non_domain_exception_falls_through(self, context):
        result = domain_exception_handler(ValueError("unexpected"), context)
        assert result is None


class TestSecurityHeadersMiddleware:
    @pytest.fixture
    def middleware(self):
        def get_response(request):
            response = Response("OK")
            response["Content-Type"] = "application/json"
            return response

        return SecurityHeadersMiddleware(get_response)

    @pytest.fixture
    def html_middleware(self):
        def get_response(request):
            response = Response("<html></html>")
            response["Content-Type"] = "text/html; charset=utf-8"
            return response

        return SecurityHeadersMiddleware(get_response)

    def test_sets_nosniff(self, middleware):
        rf = RequestFactory()
        resp = middleware(rf.get("/"))
        assert resp["X-Content-Type-Options"] == "nosniff"

    def test_sets_referrer_policy(self, middleware):
        rf = RequestFactory()
        resp = middleware(rf.get("/"))
        assert resp["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_sets_permissions_policy(self, middleware):
        rf = RequestFactory()
        resp = middleware(rf.get("/"))
        assert resp["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"

    def test_csp_set_for_html(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/"))
        assert "Content-Security-Policy" in resp

    def test_csp_not_set_for_json(self, middleware):
        rf = RequestFactory()
        resp = middleware(rf.get("/"))
        assert "Content-Security-Policy" not in resp

    def test_csp_default_for_regular_html_page(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/admin/"))
        csp = resp["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        # Default CSP must NOT include CDN sources
        assert "cdn.jsdelivr.net" not in csp

    def test_csp_relaxed_for_swagger_docs(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/docs/"))
        csp = resp["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" in csp
        assert "fonts.googleapis.com" in csp
        assert "worker-src blob:" in csp

    def test_csp_relaxed_for_redoc(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/redoc/"))
        csp = resp["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" in csp
        assert "cdn.redoc.ly" in csp

    def test_csp_swagger_includes_unsafe_inline_script(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/docs/"))
        csp = resp["Content-Security-Policy"]
        assert "'unsafe-inline'" in csp

    def test_csp_default_does_not_include_unsafe_inline_script(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/dashboard/"))
        csp = resp["Content-Security-Policy"]
        # Default CSP has unsafe-inline in style-src but NOT in script-src
        assert "script-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp

    def test_csp_relaxed_for_swagger_subpath(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/docs/extra/"))
        csp = resp["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" in csp

    def test_csp_relaxed_for_redoc_subpath(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/redoc/extra/"))
        csp = resp["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" in csp

    def test_csp_not_relaxed_for_api_schema(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/schema/"))
        csp = resp["Content-Security-Policy"]
        # /api/schema/ is not docs or redoc — should get default CSP
        assert "cdn.jsdelivr.net" not in csp

    def test_csp_relaxed_includes_connect_src(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/docs/"))
        csp = resp["Content-Security-Policy"]
        assert "connect-src 'self'" in csp

    def test_csp_relaxed_includes_font_src(self, html_middleware):
        rf = RequestFactory()
        resp = html_middleware(rf.get("/api/docs/"))
        csp = resp["Content-Security-Policy"]
        assert "font-src 'self'" in csp
        assert "fonts.gstatic.com" in csp
