"""Render-layer tests for apps.orgs.email."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.orgs.email import send_invitation_email


@pytest.fixture
def email_settings(settings):
    settings.EMAIL_FROM_ADDRESS = "noreply@saasmint.test"
    settings.RESEND_API_KEY = "re_testkey"
    settings.FRONTEND_URL = "https://app.saasmint.test"
    return settings


class TestSendInvitationEmail:
    def test_invitation_email_escapes_org_name_and_inviter_name(self, email_settings):
        """User-controlled org_name and inviter_name must be HTML-escaped.

        Both values are interpolated into raw HTML strings; without
        escaping, an org admin renaming the org to ``<script>...`` (or an
        inviter whose ``full_name`` contains markup) could smuggle script
        or styling tags into the recipient's mail client.
        """
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_invitation_email(
                email="invitee@example.com",
                token="invite-token-123",  # noqa: S106
                org_name='<script>alert("x")</script>',
                inviter_name='"><img src=x onerror=alert(1)>',
            )

        payload = mock_send.call_args.args[0]
        html = payload["html"]
        # Raw script/img markup must not survive — escape() turns the
        # angle brackets into entities, defanging the elements. The literal
        # word "onerror=" is fine without the surrounding ``<img>``: the
        # mail client renders it as text.
        assert "<script>" not in html
        assert "<img" not in html
        # Escaped forms are present.
        assert "&lt;script&gt;" in html
        assert "&lt;img" in html
        # Quotes that would have closed an attribute become entities.
        assert "&quot;" in html
        # The legitimate invite link must still be present.
        assert "invite-token-123" in html

    def test_invitation_email_contains_inviter_and_org_name_safely(self, email_settings):
        with patch("apps.email_transport.resend.Emails.send") as mock_send:
            send_invitation_email(
                email="invitee@example.com",
                token="t1",  # noqa: S106
                org_name="Acme Corp",
                inviter_name="Alice",
            )

        payload = mock_send.call_args.args[0]
        html = payload["html"]
        assert "Acme Corp" in html
        assert "Alice" in html
