"""Transactional email service using Resend."""

from __future__ import annotations

import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_from_address() -> str:
    return settings.EMAIL_FROM_ADDRESS


def _get_frontend_url() -> str:
    return settings.FRONTEND_URL


def _send(to: str, subject: str, html: str) -> None:
    """Send a single email via Resend."""
    if not resend.api_key:
        resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send(
        {
            "from": _get_from_address(),
            "to": [to],
            "subject": subject,
            "html": html,
        }
    )


def send_verification_email(email: str, token: str) -> None:
    """Send an email verification link."""
    link = f"{_get_frontend_url()}/verify-email?token={token}"
    _send(
        to=email,
        subject="Verify your email address",
        html=(
            "<p>Welcome to SaasMint! Click the link below to verify your email:</p>"
            f'<p><a href="{link}">Verify Email</a></p>'
            "<p>This link expires in 24 hours.</p>"
        ),
    )
    logger.info("Verification email sent to %s", email)


def send_password_reset_email(email: str, token: str) -> None:
    """Send a password reset link."""
    link = f"{_get_frontend_url()}/reset-password?token={token}"
    _send(
        to=email,
        subject="Reset your password",
        html=(
            "<p>You requested a password reset. Click the link below:</p>"
            f'<p><a href="{link}">Reset Password</a></p>'
            "<p>This link expires in 1 hour. If you didn't request this, "
            "ignore this email.</p>"
        ),
    )
    logger.info("Password reset email sent to %s", email)


def send_social_link_email(email: str, token: str, provider: str) -> None:
    """Send a confirmation link for attaching a new OAuth provider to the account."""
    link = f"{_get_frontend_url()}/auth/confirm-link?token={token}"
    pretty = provider.capitalize()
    _send(
        to=email,
        subject=f"Confirm linking your {pretty} account",
        html=(
            f"<p>Someone — most likely you — tried to sign in with {pretty} "
            f"using this email address. Click the link below to confirm and "
            f"link your {pretty} account to your SaasMint account:</p>"
            f'<p><a href="{link}">Confirm linking</a></p>'
            "<p>This link expires in 15 minutes. If you didn't try to link a "
            f"{pretty} account, ignore this email — your account is unchanged.</p>"
        ),
    )
    logger.info("Social link email sent to %s for %s", email, provider)
