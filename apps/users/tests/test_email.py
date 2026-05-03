"""Tests for apps.users.email — render-layer assertions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.users.email import (
    _send,
    send_password_reset_email,
    send_verification_email,
)


@pytest.fixture
def email_settings(settings):
    settings.EMAIL_FROM_ADDRESS = "noreply@saasmint.test"
    settings.FRONTEND_URL = "https://app.saasmint.test"
    settings.RESEND_API_KEY = "re_testkey"
    return settings


class TestSendVerificationEmail:
    def test_calls_resend_with_expected_envelope(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_verification_email("user@example.com", "tok_abc")

        mock_send.assert_called_once()
        payload = mock_send.call_args[0][0]
        assert payload["from"] == "noreply@saasmint.test"
        assert payload["to"] == ["user@example.com"]
        assert payload["subject"] == "Verify your email address"

    def test_html_contains_frontend_verify_link_with_token(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_verification_email("user@example.com", "tok_abc")

        html = mock_send.call_args[0][0]["html"]
        assert 'href="https://app.saasmint.test/verify-email?token=tok_abc"' in html
        assert "Verify Email" in html
        assert "24 hours" in html

    def test_token_is_embedded_verbatim(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_verification_email("user@example.com", "raw-url-safe_token.xyz")

        html = mock_send.call_args[0][0]["html"]
        assert "token=raw-url-safe_token.xyz" in html


class TestSendPasswordResetEmail:
    def test_calls_resend_with_expected_envelope(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_password_reset_email("user@example.com", "tok_reset")

        mock_send.assert_called_once()
        payload = mock_send.call_args[0][0]
        assert payload["from"] == "noreply@saasmint.test"
        assert payload["to"] == ["user@example.com"]
        assert payload["subject"] == "Reset your password"

    def test_html_contains_frontend_reset_link_with_token(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_password_reset_email("user@example.com", "tok_reset")

        html = mock_send.call_args[0][0]["html"]
        assert 'href="https://app.saasmint.test/reset-password?token=tok_reset"' in html
        assert "Reset Password" in html
        assert "1 hour" in html

    def test_warns_if_unsolicited(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            send_password_reset_email("user@example.com", "tok_reset")

        html = mock_send.call_args[0][0]["html"]
        assert "ignore" in html.lower()


class TestSendHelper:
    def test_sets_api_key_when_unset(self, email_settings):
        import resend

        original = resend.api_key
        resend.api_key = None
        try:
            with patch("apps.users.email.resend.Emails.send"):
                _send("user@example.com", "Subject", "<p>Body</p>")
            assert resend.api_key == "re_testkey"
        finally:
            resend.api_key = original

    def test_does_not_override_existing_api_key(self, email_settings):
        import resend

        original = resend.api_key
        resend.api_key = "re_already_set"
        try:
            with patch("apps.users.email.resend.Emails.send"):
                _send("user@example.com", "Subject", "<p>Body</p>")
            assert resend.api_key == "re_already_set"
        finally:
            resend.api_key = original

    def test_forwards_payload_to_resend(self, email_settings):
        with patch("apps.users.email.resend.Emails.send") as mock_send:
            _send("user@example.com", "Hello", "<p>Body</p>")

        payload = mock_send.call_args[0][0]
        assert payload["to"] == ["user@example.com"]
        assert payload["subject"] == "Hello"
        assert payload["html"] == "<p>Body</p>"
        assert payload["from"] == "noreply@saasmint.test"


class TestEmailLogging:
    def test_verification_email_logs_recipient(
        self, email_settings, caplog: pytest.LogCaptureFixture
    ):
        with patch("apps.users.email.resend.Emails.send"):
            with caplog.at_level("INFO", logger="apps.users.email"):
                send_verification_email("user@example.com", "tok_abc")

        assert any("user@example.com" in r.message for r in caplog.records)

    def test_reset_email_logs_recipient(self, email_settings, caplog: pytest.LogCaptureFixture):
        with patch("apps.users.email.resend.Emails.send"):
            with caplog.at_level("INFO", logger="apps.users.email"):
                send_password_reset_email("user@example.com", "tok_reset")

        assert any("user@example.com" in r.message for r in caplog.records)
