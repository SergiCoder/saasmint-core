"""Tests for User model, UserManager, and cache invalidation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.core.cache import cache

from apps.users.models import AccountType, User


@pytest.mark.django_db
class TestUserManager:
    def test_create_user_sets_fields(self):
        user = User.objects.create_user(
            email="manager@example.com",
            supabase_uid="sup_mgr",
            full_name="Manager User",
        )
        assert user.email == "manager@example.com"
        assert user.supabase_uid == "sup_mgr"
        assert user.full_name == "Manager User"
        assert user.is_active is True
        assert user.is_staff is False
        assert user.has_usable_password() is False

    def test_create_user_normalizes_email(self):
        user = User.objects.create_user(
            email="Test@EXAMPLE.COM",
            supabase_uid="sup_norm",
        )
        assert user.email == "Test@example.com"

    def test_create_superuser_sets_flags(self):
        user = User.objects.create_superuser(email="admin@example.com")
        assert user.is_staff is True
        assert user.is_superuser is True
        assert user.is_active is True
        assert user.supabase_uid == "superuser"

    def test_create_superuser_with_custom_uid(self):
        user = User.objects.create_superuser(email="admin2@example.com", supabase_uid="sup_admin")
        assert user.supabase_uid == "sup_admin"


@pytest.mark.django_db
class TestUserModel:
    def test_str_returns_email(self):
        user = User.objects.create_user(email="str@example.com", supabase_uid="sup_str")
        assert str(user) == "str@example.com"

    def test_default_account_type(self):
        user = User.objects.create_user(email="default@example.com", supabase_uid="sup_default")
        assert user.account_type == AccountType.PERSONAL

    def test_save_clears_cache_when_deactivated(self):
        user = User.objects.create_user(email="deact@example.com", supabase_uid="sup_deact")
        cache_key = f"auth_user:{user.supabase_uid}"
        cache.set(cache_key, user, timeout=60)
        assert cache.get(cache_key) is not None

        user.is_active = False
        user.save()
        assert cache.get(cache_key) is None

    def test_save_clears_cache_when_soft_deleted(self):
        user = User.objects.create_user(email="softdel@example.com", supabase_uid="sup_softdel")
        cache_key = f"auth_user:{user.supabase_uid}"
        cache.set(cache_key, user, timeout=60)
        assert cache.get(cache_key) is not None

        user.deleted_at = datetime.now(UTC)
        user.save()
        assert cache.get(cache_key) is None

    def test_save_always_clears_cache(self):
        user = User.objects.create_user(email="active@example.com", supabase_uid="sup_active")
        cache_key = f"auth_user:{user.supabase_uid}"
        cache.set(cache_key, user, timeout=60)

        user.full_name = "Updated"
        user.save()
        # Cache is always invalidated on save to prevent stale reads
        assert cache.get(cache_key) is None

    def test_uuid_primary_key(self):
        user = User.objects.create_user(email="uuid@example.com", supabase_uid="sup_uuid")
        import uuid

        assert isinstance(user.pk, uuid.UUID)
