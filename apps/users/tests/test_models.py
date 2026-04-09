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
            password="testpass123",  # noqa: S106
            full_name="Manager User",
        )
        assert user.email == "manager@example.com"
        assert user.full_name == "Manager User"
        assert user.is_active is True
        assert user.is_staff is False
        assert user.has_usable_password() is True

    def test_create_user_no_password(self):
        user = User.objects.create_user(
            email="nopass@example.com",
            full_name="No Password",
        )
        assert user.has_usable_password() is False

    def test_create_user_normalizes_email(self):
        user = User.objects.create_user(
            email="Test@EXAMPLE.COM",
            full_name="Norm User",
        )
        assert user.email == "Test@example.com"

    def test_create_user_empty_email_raises(self):
        with pytest.raises(ValueError, match="Email is required"):
            User.objects.create_user(email="", full_name="No Email")

    def test_create_superuser_sets_flags(self):
        user = User.objects.create_superuser(email="admin@example.com", password="adminpass")  # noqa: S106
        assert user.is_staff is True
        assert user.is_superuser is True
        assert user.is_active is True


@pytest.mark.django_db
class TestUserModel:
    def test_str_returns_email(self):
        user = User.objects.create_user(email="str@example.com", full_name="Str User")
        assert str(user) == "str@example.com"

    def test_default_account_type(self):
        user = User.objects.create_user(email="default@example.com", full_name="Default User")
        assert user.account_type == AccountType.PERSONAL

    def test_save_clears_cache_when_deactivated(self):
        user = User.objects.create_user(email="deact@example.com", full_name="Deact User")
        cache_key = f"auth_user:{user.id}"
        cache.set(cache_key, user, timeout=60)
        assert cache.get(cache_key) is not None

        user.is_active = False
        user.save()
        assert cache.get(cache_key) is None

    def test_save_clears_cache_when_soft_deleted(self):
        user = User.objects.create_user(email="softdel@example.com", full_name="Soft Del")
        cache_key = f"auth_user:{user.id}"
        cache.set(cache_key, user, timeout=60)
        assert cache.get(cache_key) is not None

        user.deleted_at = datetime.now(UTC)
        user.save()
        assert cache.get(cache_key) is None

    def test_save_always_clears_cache(self):
        user = User.objects.create_user(email="active@example.com", full_name="Active User")
        cache_key = f"auth_user:{user.id}"
        cache.set(cache_key, user, timeout=60)

        user.full_name = "Updated"
        user.save()
        # Cache is always invalidated on save to prevent stale reads
        assert cache.get(cache_key) is None

    def test_uuid_primary_key(self):
        user = User.objects.create_user(email="uuid@example.com", full_name="UUID User")
        import uuid

        assert isinstance(user.pk, uuid.UUID)

    def test_new_fields_default_to_none(self):
        user = User.objects.create_user(email="defaults@example.com", full_name="Defaults User")
        assert user.phone_prefix is None
        assert user.phone is None
        assert user.timezone is None
        assert user.job_title is None
        assert user.pronouns is None
        assert user.bio is None
        assert user.scheduled_deletion_at is None

    def test_new_fields_can_be_set(self):
        user = User.objects.create_user(
            email="fields@example.com",
            full_name="Field User",
            phone_prefix="+34",
            phone="612345678",
            timezone="Europe/Madrid",
            job_title="Engineer",
            pronouns="they/them",
            bio="A brief bio",
        )
        assert user.phone_prefix == "+34"
        assert user.phone == "612345678"
        assert user.timezone == "Europe/Madrid"
        assert user.job_title == "Engineer"
        assert user.pronouns == "they/them"
        assert user.bio == "A brief bio"

    def test_scheduled_deletion_at_can_be_set_and_cleared(self):
        user = User.objects.create_user(email="sched@example.com", full_name="Sched User")
        assert user.scheduled_deletion_at is None

        user.scheduled_deletion_at = datetime.now(UTC)
        user.save(update_fields=["scheduled_deletion_at"])
        user.refresh_from_db()
        assert user.scheduled_deletion_at is not None

        user.scheduled_deletion_at = None
        user.save(update_fields=["scheduled_deletion_at"])
        user.refresh_from_db()
        assert user.scheduled_deletion_at is None
