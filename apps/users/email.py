"""Transactional email service using Resend."""

from __future__ import annotations

import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_from_address() -> str:
    return getattr(settings, "EMAIL_FROM_ADDRESS", "noreply@saasmint.com")


def _get_frontend_url() -> str:
    return getattr(settings, "FRONTEND_URL", "http://localhost:3000")


def send_verification_email(email: str, token: str) -> None:
    """Send an email verification link."""
    resend.api_key = settings.RESEND_API_KEY
    frontend_url = _get_frontend_url()
    link = f"{frontend_url}/verify-email?token={token}"

    resend.Emails.send(
        {
            "from": _get_from_address(),
            "to": [email],
            "subject": "Verify your email address",
            "html": (
                f"<p>Welcome to SaasMint! Click the link below to verify your email:</p>"
                f'<p><a href="{link}">Verify Email</a></p>'
                f"<p>This link expires in 24 hours.</p>"
            ),
        }
    )
    logger.info("Verification email sent to %s", email)


def send_password_reset_email(email: str, token: str) -> None:
    """Send a password reset link."""
    resend.api_key = settings.RESEND_API_KEY
    frontend_url = _get_frontend_url()
    link = f"{frontend_url}/reset-password?token={token}"

    resend.Emails.send(
        {
            "from": _get_from_address(),
            "to": [email],
            "subject": "Reset your password",
            "html": (
                f"<p>You requested a password reset. Click the link below:</p>"
                f'<p><a href="{link}">Reset Password</a></p>'
                f"<p>This link expires in 1 hour. If you didn't request this, "
                f"ignore this email.</p>"
            ),
        }
    )
    logger.info("Password reset email sent to %s", email)
