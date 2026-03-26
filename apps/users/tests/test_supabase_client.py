"""Tests for the Supabase client singleton."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import override_settings


@override_settings(
    SUPABASE_URL="https://test.supabase.co",
    SUPABASE_ANON_KEY="test-anon-key",
)
@patch("config.supabase.create_client")
def test_get_supabase_client_creates_once(mock_create: MagicMock) -> None:
    import config.supabase as mod

    mod.get_supabase_client.cache_clear()
    sentinel = MagicMock()
    mock_create.return_value = sentinel

    first = mod.get_supabase_client()
    second = mod.get_supabase_client()

    assert first is sentinel
    assert second is sentinel
    mock_create.assert_called_once_with("https://test.supabase.co", "test-anon-key")

    # Reset for other tests
    mod.get_supabase_client.cache_clear()
