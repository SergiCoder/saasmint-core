"""Supabase client singleton for Django."""

from __future__ import annotations

from functools import lru_cache

from django.conf import settings
from supabase import Client, create_client


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Return a cached Supabase client, created once on first call."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
