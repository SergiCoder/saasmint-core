"""Celery tasks for user account operations."""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync

from config.celery import app

logger = logging.getLogger(__name__)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def process_scheduled_deletions() -> None:
    """Hard-delete users whose scheduled_deletion_at has passed."""
    from saasmint_core.services.gdpr import execute_account_deletion

    from apps.billing.repositories import (
        DjangoStripeCustomerRepository,
        DjangoSubscriptionRepository,
    )
    from apps.users.repositories import DjangoUserRepository

    user_repo = DjangoUserRepository()
    customer_repo = DjangoStripeCustomerRepository()
    subscription_repo = DjangoSubscriptionRepository()

    pending_users = async_to_sync(user_repo.list_pending_deletions)()

    for user in pending_users:
        try:
            async_to_sync(execute_account_deletion)(
                user_id=user.id,
                user_repo=user_repo,
                customer_repo=customer_repo,
                subscription_repo=subscription_repo,
            )
            logger.info("Executed scheduled deletion for user %s", user.id)
        except Exception:
            logger.exception("Failed scheduled deletion for user %s", user.id)
