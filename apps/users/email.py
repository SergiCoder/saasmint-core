"""Transactional email service using Resend."""

from __future__ import annotations

import logging

from django.conf import settings

from apps.email_transport import send_email

logger = logging.getLogger(__name__)


def send_verification_email(email: str, token: str) -> None:
    """Send an email verification link."""
    link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    send_email(
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
    from apps.users.authentication import PASSWORD_RESET_LIFETIME

    link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    lifetime_minutes = int(PASSWORD_RESET_LIFETIME.total_seconds()) // 60
    send_email(
        to=email,
        subject="Reset your password",
        html=(
            "<p>You requested a password reset. Click the link below:</p>"
            f'<p><a href="{link}">Reset Password</a></p>'
            f"<p>This link expires in {lifetime_minutes} minutes. If you didn't "
            "request this, ignore this email.</p>"
        ),
    )
    logger.info("Password reset email sent to %s", email)


def send_social_link_email(email: str, token: str, provider: str) -> None:
    """Send a confirmation link for attaching a new OAuth provider to the account."""
    from apps.users.authentication import SOCIAL_LINK_LIFETIME

    link = f"{settings.FRONTEND_URL}/auth/confirm-link?token={token}"
    pretty = provider.capitalize()
    lifetime_minutes = int(SOCIAL_LINK_LIFETIME.total_seconds()) // 60
    send_email(
        to=email,
        subject=f"Confirm linking your {pretty} account",
        html=(
            f"<p>Someone — most likely you — tried to sign in with {pretty} "
            f"using this email address. Click the link below to confirm and "
            f"link your {pretty} account to your SaasMint account:</p>"
            f'<p><a href="{link}">Confirm linking</a></p>'
            f"<p>This link expires in {lifetime_minutes} minutes. If you didn't try to link a "
            f"{pretty} account, ignore this email — your account is unchanged.</p>"
        ),
    )
    logger.info("Social link email sent to %s for %s", email, provider)
