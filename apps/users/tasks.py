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

    from django.db.models import Subquery

    from apps.orgs.models import OrgMember
    from apps.users.models import AccountType, User

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    orphans = User.objects.filter(
        account_type=AccountType.ORG_MEMBER,
        created_at__lt=cutoff,
    ).exclude(
        id__in=Subquery(OrgMember.objects.values("user_id")),
    )
    # _, details has a mapping but we only need the aggregate count for logging
    deleted, _ = orphans.delete()
    if deleted:
        logger.info("Cleaned up %d orphaned org-member accounts", deleted)


_REFRESH_TOKEN_DELETE_BATCH = 10_000


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def cleanup_expired_refresh_tokens() -> None:
    """Delete refresh token rows whose expires_at has passed.

    Expired tokens are already rejected at verification time, but the rows
    accumulate indefinitely without a cleanup task. Delete in bounded batches
    so a backlog of millions of expired rows can't take out a long table-wide
    lock.
    """
    from datetime import UTC, datetime

    from apps.users.models import RefreshToken

    now = datetime.now(UTC)
    total_deleted = 0
    while True:
        # Use an id-subquery so the delete is bounded by the batch size; the
        # ORM doesn't accept LIMIT directly on .delete().
        ids = list(
            RefreshToken.objects.filter(expires_at__lt=now).values_list("id", flat=True)[
                :_REFRESH_TOKEN_DELETE_BATCH
            ]
        )
        if not ids:
            break
        deleted, _ = RefreshToken.objects.filter(id__in=ids).delete()
        total_deleted += deleted
        if deleted < _REFRESH_TOKEN_DELETE_BATCH:
            break

    if total_deleted:
        logger.info("Pruned %d expired refresh tokens", total_deleted)
