"""Tests for apps.marketing.email — render-layer assertions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.marketing.email import redact_email, send_marketing_inquiry_email


@pytest.fixture
def email_settings(settings):
    settings.EMAIL_FROM_ADDRESS = "noreply@saasmint.test"
    settings.RESEND_API_KEY = "re_testkey"
    return settings


class TestRedactEmail:
    def test_redacts_local_part(self):
        assert redact_email("jane@example.com") == "j***@example.com"

    def test_short_local_part(self):
        assert redact_email("a@example.com") == "a***@example.com"

    def test_handles_missing_at_sign(self):
        assert redact_email("not-an-email") == "***"


class TestSendMarketingInquiryEmail:
    def test_calls_resend_with_text_body_not_html(self, email_settings):
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_marketing_inquiry_email(
                to="ops@saasmint.test",
                source="landing-cta",
                sender="visitor@example.com",
                message="",
            )

        payload = mock_send.call_args[0][0]
        assert payload["from"] == "noreply@saasmint.test"
        assert payload["to"] == ["ops@saasmint.test"]
        assert "text" in payload
        assert "html" not in payload

    def test_subject_includes_source_and_sender(self, email_settings):
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_marketing_inquiry_email(
                to="ops@saasmint.test",
                source="contact-page",
                sender="visitor@example.com",
                message="hi",
            )

        assert (
            mock_send.call_args[0][0]["subject"]
            == "[SaaSmint] contact-page: visitor@example.com"
        )

    def test_body_includes_source_sender_and_message(self, email_settings):
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_marketing_inquiry_email(
                to="ops@saasmint.test",
                source="contact-page",
                sender="visitor@example.com",
                message="please call me back",
            )

        text = mock_send.call_args[0][0]["text"]
        assert "Source: contact-page" in text
        assert "From:   visitor@example.com" in text
        assert "please call me back" in text

    def test_body_renders_no_message_placeholder(self, email_settings):
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_marketing_inquiry_email(
                to="ops@saasmint.test",
                source="landing-cta",
                sender="visitor@example.com",
                message="",
            )

        assert "(no message)" in mock_send.call_args[0][0]["text"]
