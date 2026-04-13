"""Celery tasks for user account operations."""

from __future__ import annotations

import logging

from config.celery import app

logger = logging.getLogger(__name__)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_verification_email_task(email: str, token: str) -> None:
    """Send email verification link via Resend (async-safe)."""
    from apps.users.email import send_verification_email

    send_verification_email(email, token)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_password_reset_email_task(email: str, token: str) -> None:
    """Send password reset link via Resend (async-safe)."""
    from apps.users.email import send_password_reset_email

    send_password_reset_email(email, token)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def cleanup_orphaned_org_accounts() -> None:
    """Delete org_member accounts that never completed checkout.

    Targets users with account_type=ORG_MEMBER who have no org membership
    and were created more than 24 hours ago.
    """
    from datetime import UTC, datetime, timedelta

    from apps.orgs.models import OrgMember
    from apps.users.models import AccountType, User

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    orphans = User.objects.filter(
        account_type=AccountType.ORG_MEMBER,
        created_at__lt=cutoff,
    ).exclude(
        id__in=OrgMember.objects.values_list("user_id", flat=True),
    )
    count = orphans.count()
    if count:
        orphans.delete()
        logger.info("Cleaned up %d orphaned org-member accounts", count)
