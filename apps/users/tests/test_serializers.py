"""Tests for UserSerializer and UpdateUserSerializer."""

from __future__ import annotations

import pytest

from apps.users.models import User
from apps.users.serializers import UpdateUserSerializer, UserSerializer


@pytest.mark.django_db
class TestUserSerializer:
    def test_serializes_expected_fields(self):
        user = User.objects.create_user(
            email="ser@example.com",
            supabase_uid="sup_ser",
            full_name="Ser User",
        )
        data = UserSerializer(user).data
        assert data["email"] == "ser@example.com"
        assert data["full_name"] == "Ser User"
        assert "id" in data
        assert "created_at" in data
        # Sensitive fields should not leak
        assert "password" not in data
        assert "supabase_uid" not in data
        assert "deleted_at" not in data
        assert "is_active" not in data
        assert "is_staff" not in data

    def test_all_fields_are_read_only(self):
        """UserSerializer should not allow writes via its fields."""
        assert set(UserSerializer.Meta.read_only_fields) == set(UserSerializer.Meta.fields)


class TestUpdateUserSerializer:
    def test_valid_partial_update(self):
        ser = UpdateUserSerializer(data={"full_name": "New Name"})
        assert ser.is_valid(), ser.errors

    def test_valid_all_fields(self):
        ser = UpdateUserSerializer(
            data={
                "full_name": "Full Name",
                "avatar_url": "https://example.com/avatar.png",
                "preferred_locale": "en",
                "preferred_currency": "usd",
            }
        )
        assert ser.is_valid(), ser.errors

    def test_empty_data_is_valid(self):
        ser = UpdateUserSerializer(data={})
        assert ser.is_valid(), ser.errors

    def test_null_full_name_is_valid(self):
        ser = UpdateUserSerializer(data={"full_name": None})
        assert ser.is_valid(), ser.errors

    def test_null_avatar_url_is_valid(self):
        ser = UpdateUserSerializer(data={"avatar_url": None})
        assert ser.is_valid(), ser.errors

    def test_invalid_avatar_url_rejected(self):
        ser = UpdateUserSerializer(data={"avatar_url": "not-a-url"})
        assert not ser.is_valid()
        assert "avatar_url" in ser.errors

    def test_unsupported_locale_rejected(self):
        ser = UpdateUserSerializer(data={"preferred_locale": "xx-FAKE"})
        assert not ser.is_valid()
        assert "preferred_locale" in ser.errors

    def test_supported_locale_accepted(self):
        ser = UpdateUserSerializer(data={"preferred_locale": "es"})
        assert ser.is_valid(), ser.errors

    def test_unsupported_currency_rejected(self):
        ser = UpdateUserSerializer(data={"preferred_currency": "zzz"})
        assert not ser.is_valid()
        assert "preferred_currency" in ser.errors

    def test_supported_currency_accepted(self):
        ser = UpdateUserSerializer(data={"preferred_currency": "eur"})
        assert ser.is_valid(), ser.errors

    def test_full_name_max_length(self):
        ser = UpdateUserSerializer(data={"full_name": "x" * 256})
        assert not ser.is_valid()
        assert "full_name" in ser.errors
