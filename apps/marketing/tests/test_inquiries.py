"""Tests for the marketing inquiries endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

URL = "/api/v1/marketing/inquiries/"
MOCK_INBOX = "ops@saasmint.test"

# Mirror the auth-views relaxed throttling baseline; individual tests opt in to
# tighter rates for the rate-limit assertions.
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {"auth": "1000/hour", "marketing_inquiries": "1000/hour"},
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


@pytest.fixture(autouse=True)
def _set_inbox(monkeypatch):
    monkeypatch.setenv("MARKETING_INQUIRIES_TO", MOCK_INBOX)


@pytest.fixture
def api():
    return APIClient()


class TestMarketingInquiryHappyPaths:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_landing_cta_email_only_returns_204(self, mock_delay, api):
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "landing-cta", "honeypot": ""},
            format="json",
        )
        assert resp.status_code == 204
        mock_delay.assert_called_once_with(
            to=MOCK_INBOX,
            source="landing-cta",
            sender="visitor@example.com",
            message="",
        )

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_contact_page_with_message_returns_204(self, mock_delay, api):
        resp = api.post(
            URL,
            {
                "email": "visitor@example.com",
                "source": "contact-page",
                "message": "Hello, I have a question.",
                "honeypot": "",
            },
            format="json",
        )
        assert resp.status_code == 204
        mock_delay.assert_called_once_with(
            to=MOCK_INBOX,
            source="contact-page",
            sender="visitor@example.com",
            message="Hello, I have a question.",
        )

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_message_whitespace_is_stripped(self, mock_delay, api):
        resp = api.post(
            URL,
            {
                "email": "visitor@example.com",
                "source": "contact-page",
                "message": "   trimmed   ",
            },
            format="json",
        )
        assert resp.status_code == 204
        assert mock_delay.call_args.kwargs["message"] == "trimmed"


class TestMarketingInquiryValidation:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_missing_email_returns_400(self, mock_delay, api):
        resp = api.post(URL, {"source": "landing-cta"}, format="json")
        assert resp.status_code == 400
        assert "email" in resp.data
        mock_delay.assert_not_called()

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_invalid_email_format_returns_400(self, mock_delay, api):
        resp = api.post(
            URL,
            {"email": "not-an-email", "source": "landing-cta"},
            format="json",
        )
        assert resp.status_code == 400
        mock_delay.assert_not_called()

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_message_over_limit_returns_400(self, mock_delay, api):
        resp = api.post(
            URL,
            {
                "email": "visitor@example.com",
                "source": "contact-page",
                "message": "x" * 5001,
            },
            format="json",
        )
        assert resp.status_code == 400
        mock_delay.assert_not_called()

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_unknown_source_returns_400(self, mock_delay, api):
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "twitter-dm"},
            format="json",
        )
        assert resp.status_code == 400
        mock_delay.assert_not_called()

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_contact_page_without_message_returns_400(self, mock_delay, api):
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "contact-page"},
            format="json",
        )
        assert resp.status_code == 400
        assert "message" in resp.data
        mock_delay.assert_not_called()


class TestMarketingInquiryHoneypot:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_nonempty_honeypot_silently_drops(self, mock_delay, api):
        resp = api.post(
            URL,
            {
                "email": "visitor@example.com",
                "source": "landing-cta",
                "honeypot": "i-am-a-bot",
            },
            format="json",
        )
        assert resp.status_code == 204
        mock_delay.assert_not_called()


class TestMarketingInquiryMisconfiguration:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_missing_inbox_env_returns_500(self, mock_delay, api, monkeypatch):
        monkeypatch.delenv("MARKETING_INQUIRIES_TO", raising=False)
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "landing-cta"},
            format="json",
        )
        assert resp.status_code == 500
        assert resp.data["code"] == "marketing_inbox_unconfigured"
        mock_delay.assert_not_called()

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_blank_inbox_env_returns_500(self, mock_delay, api, monkeypatch):
        monkeypatch.setenv("MARKETING_INQUIRIES_TO", "   ")
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "landing-cta"},
            format="json",
        )
        assert resp.status_code == 500
        mock_delay.assert_not_called()


class TestMarketingInquiryRateLimit:
    """The endpoint has its own ``marketing_inquiries`` throttle scope (3 / 10 min)."""

    def test_view_uses_marketing_throttle(self):
        """Pin the wiring; rate-limit *behavior* is exercised by DRF's own test suite.

        Asserting throttle wiring rather than driving 429s from a test — DRF's
        ``api_settings`` caches throttle rates in-process and resetting them
        through ``pytest-django``'s ``settings`` fixture is unreliable across
        the autouse-reset boundary.
        """
        from apps.marketing.throttling import MarketingInquiryThrottle
        from apps.marketing.views import MarketingInquiryView

        view = MarketingInquiryView()
        assert MarketingInquiryThrottle in view.throttle_classes

    def test_throttle_parses_multi_unit_period(self):
        """The custom parser accepts ``N/<count><unit>`` rates (e.g. ``3/10minute``)."""
        from apps.marketing.throttling import MarketingInquiryThrottle

        t = MarketingInquiryThrottle()
        assert t.parse_rate("3/10minute") == (3, 600)
        assert t.parse_rate("5/hour") == (5, 3600)
        assert t.parse_rate("3/m") == (3, 60)
        assert t.parse_rate(None) == (None, None)


class TestMarketingInquiryResponseShape:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_204_has_empty_body(self, _mock_delay, api):
        resp = api.post(
            URL,
            {"email": "visitor@example.com", "source": "landing-cta"},
            format="json",
        )
        assert resp.status_code == 204
        assert not resp.content


class TestMarketingInquiryLogging:
    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_redacts_email_in_logs(self, _mock_delay, api, caplog):
        with caplog.at_level("INFO", logger="apps.marketing.views"):
            api.post(
                URL,
                {"email": "jane@example.com", "source": "landing-cta"},
                format="json",
            )
        log_text = " ".join(r.message for r in caplog.records)
        assert "j***@example.com" in log_text
        assert "jane@example.com" not in log_text

    @patch("apps.marketing.views.send_marketing_inquiry_email_task.delay")
    def test_does_not_log_message_body(self, _mock_delay, api, caplog):
        secret = "super-secret-customer-message-9c1f"
        with caplog.at_level("INFO", logger="apps.marketing.views"):
            api.post(
                URL,
                {
                    "email": "jane@example.com",
                    "source": "contact-page",
                    "message": secret,
                },
                format="json",
            )
        log_text = " ".join(r.message for r in caplog.records)
        assert secret not in log_text
