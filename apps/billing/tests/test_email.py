"""Tests for apps.billing.email — render-layer assertions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.billing.email import (
    send_subscription_cancel_resumed,
    send_subscription_cancel_scheduled,
)


@pytest.fixture
def email_settings(settings):
    settings.EMAIL_FROM_ADDRESS = "noreply@saasmint.test"
    settings.RESEND_API_KEY = "re_testkey"
    return settings


class TestSendSubscriptionCancelScheduled:
    def test_calls_transport_with_expected_envelope(self, email_settings):
        with patch("apps.billing.email.send_email") as mock_send:
            send_subscription_cancel_scheduled("finance@example.com", "Team Pro Monthly")

        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "finance@example.com"
        assert kwargs["subject"] == "Your subscription is scheduled to cancel"

    def test_html_embeds_subscription_label(self, email_settings):
        with patch("apps.billing.email.send_email") as mock_send:
            send_subscription_cancel_scheduled("finance@example.com", "Team Pro Monthly")

        html = mock_send.call_args.kwargs["html"]
        assert "Team Pro Monthly" in html
        assert "scheduled to cancel" in html
        assert "resume" in html.lower()


class TestSendSubscriptionCancelResumed:
    def test_calls_transport_with_expected_envelope(self, email_settings):
        with patch("apps.billing.email.send_email") as mock_send:
            send_subscription_cancel_resumed("finance@example.com", "Team Pro Monthly")

        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "finance@example.com"
        assert kwargs["subject"] == "Your subscription cancellation was reverted"

    def test_html_embeds_subscription_label(self, email_settings):
        with patch("apps.billing.email.send_email") as mock_send:
            send_subscription_cancel_resumed("finance@example.com", "Team Pro Monthly")

        html = mock_send.call_args.kwargs["html"]
        assert "Team Pro Monthly" in html
        assert "cleared" in html or "continue to renew" in html


class TestEmailLogging:
    def test_scheduled_email_logs_recipient(self, email_settings, caplog: pytest.LogCaptureFixture):
        with patch("apps.billing.email.send_email"):
            with caplog.at_level("INFO", logger="apps.billing.email"):
                send_subscription_cancel_scheduled("finance@example.com", "Label")

        assert any("finance@example.com" in r.message for r in caplog.records)

    def test_resumed_email_logs_recipient(self, email_settings, caplog: pytest.LogCaptureFixture):
        with patch("apps.billing.email.send_email"):
            with caplog.at_level("INFO", logger="apps.billing.email"):
                send_subscription_cancel_resumed("finance@example.com", "Label")

        assert any("finance@example.com" in r.message for r in caplog.records)
