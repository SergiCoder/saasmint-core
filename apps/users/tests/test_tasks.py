"""Tests for Celery tasks in the users app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from apps.users.models import User


@pytest.fixture
def user_pending_deletion(db) -> User:
    """Create a user whose scheduled_deletion_at has passed."""
    user = User.objects.create_user(
        email="pending@example.com",
        full_name="Pending User",
    )
    user.scheduled_deletion_at = datetime.now(UTC) - timedelta(hours=1)
    user.save(update_fields=["scheduled_deletion_at"])
    return user


@pytest.mark.django_db
class TestProcessScheduledDeletions:
    @patch("apps.users.tasks.async_to_sync")
    def test_processes_pending_users(self, mock_async_to_sync, user_pending_deletion):
        """Task should call execute_account_deletion for each pending user."""
        # Mock async_to_sync to return callables
        mock_list_pending = MagicMock(return_value=[MagicMock(id=user_pending_deletion.id)])
        mock_execute = MagicMock()

        call_count = 0

        def fake_async_to_sync(coro_func):
            nonlocal call_count
            call_count += 1
            # First call is list_pending_deletions, second is execute_account_deletion
            if call_count == 1:
                return mock_list_pending
            return mock_execute

        mock_async_to_sync.side_effect = fake_async_to_sync

        from apps.users.tasks import process_scheduled_deletions

        process_scheduled_deletions()

        mock_list_pending.assert_called_once()
        mock_execute.assert_called_once()

    @patch("apps.users.tasks.async_to_sync")
    def test_continues_on_individual_failure(self, mock_async_to_sync):
        """If one user's deletion fails, others should still be processed."""
        user1 = MagicMock(id="user1")
        user2 = MagicMock(id="user2")
        mock_list_pending = MagicMock(return_value=[user1, user2])
        mock_execute = MagicMock(side_effect=[RuntimeError("boom"), None])

        call_count = 0

        def fake_async_to_sync(coro_func):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_list_pending
            return mock_execute

        mock_async_to_sync.side_effect = fake_async_to_sync

        from apps.users.tasks import process_scheduled_deletions

        # Should not raise despite first user failing
        process_scheduled_deletions()

        assert mock_execute.call_count == 2

    @patch("apps.users.tasks.async_to_sync")
    def test_no_pending_users_is_noop(self, mock_async_to_sync):
        """When no users are pending deletion, task completes without calling execute."""
        mock_list_pending = MagicMock(return_value=[])
        mock_execute = MagicMock()

        call_count = 0

        def fake_async_to_sync(coro_func):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_list_pending
            return mock_execute

        mock_async_to_sync.side_effect = fake_async_to_sync

        from apps.users.tasks import process_scheduled_deletions

        process_scheduled_deletions()

        mock_list_pending.assert_called_once()
        mock_execute.assert_not_called()
