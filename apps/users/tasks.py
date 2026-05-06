"""Celery tasks for user account operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config.celery import app

if TYPE_CHECKING:
    from django.db.models import Model

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
def send_social_link_email_task(email: str, token: str, provider: str) -> None:
    """Send OAuth provider link confirmation via Resend (async-safe)."""
    from apps.users.email import send_social_link_email

    send_social_link_email(email, token, provider)


_EXPIRED_ROW_DELETE_BATCH = 10_000


def _delete_expired_rows(model: type[Model], label: str) -> None:
    """Delete rows whose ``expires_at`` has passed, in bounded batches.

    Expired rows are already rejected at verification time, but accumulate
    indefinitely without a cleanup task. Batched deletes prevent a backlog of
    millions of rows from taking out a long table-wide lock. The ORM doesn't
    accept LIMIT directly on ``.delete()``, so an id-subquery bounds each
    batch.
    """
    from datetime import UTC, datetime

    manager = model._default_manager
    now = datetime.now(UTC)
    total_deleted = 0
    while True:
        ids = list(
            manager.filter(expires_at__lt=now).values_list("id", flat=True)[
                :_EXPIRED_ROW_DELETE_BATCH
            ]
        )
        if not ids:
            break
        deleted, _ = manager.filter(id__in=ids).delete()
        total_deleted += deleted
        if deleted < _EXPIRED_ROW_DELETE_BATCH:
            break

    if total_deleted:
        logger.info("Pruned %d expired %s", total_deleted, label)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def cleanup_expired_refresh_tokens() -> None:
    """Delete refresh token rows whose expires_at has passed."""
    from apps.users.models import RefreshToken

    _delete_expired_rows(RefreshToken, "refresh tokens")


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def cleanup_expired_social_link_requests() -> None:
    """Delete SocialLinkRequest rows whose expires_at has passed."""
    from apps.users.models import SocialLinkRequest

    _delete_expired_rows(SocialLinkRequest, "social link requests")
