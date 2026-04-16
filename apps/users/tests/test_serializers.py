"""Tests for UserSerializer and UpdateUserSerializer."""

from __future__ import annotations

import pytest

from apps.users.models import SocialAccount, User
from apps.users.serializers import UpdateUserSerializer, UserSerializer


@pytest.mark.django_db
class TestUserSerializer:
    def test_serializes_expected_fields(self):
        user = User.objects.create_user(
            email="ser@example.com",
            full_name="Ser User",
        )
        data = UserSerializer(user).data
        assert data["email"] == "ser@example.com"
        assert data["full_name"] == "Ser User"
        assert "id" in data
        assert "created_at" in data
        # Sensitive fields should not leak
        assert "password" not in data
        assert "deleted_at" not in data
        assert "is_active" not in data
        assert "is_staff" not in data

    def test_all_fields_are_read_only(self):
        """UserSerializer should not allow writes via its fields."""
        assert set(UserSerializer.Meta.read_only_fields) == set(UserSerializer.Meta.fields)

    def test_new_profile_fields_serialized(self):
        user = User.objects.create_user(
            email="profile@example.com",
            full_name="Profile User",
            phone_prefix="+34",
            phone="612345678",
            timezone="Europe/Madrid",
            job_title="Engineer",
            pronouns="they/them",
            bio="Hello world",
        )
        data = UserSerializer(user).data
        assert data["phone"] == {"prefix": "+34", "number": "612345678"}
        assert data["timezone"] == "Europe/Madrid"
        assert data["job_title"] == "Engineer"
        assert data["pronouns"] == "they/them"
        assert data["bio"] == "Hello world"
        assert "updated_at" in data

    def test_phone_null_when_both_prefix_and_number_are_none(self):
        """When phone_prefix and phone are both None, phone should serialize as None."""
        user = User.objects.create_user(
            email="nophone@example.com",
            full_name="No Phone",
        )
        data = UserSerializer(user).data
        assert data["phone"] is None

    def test_phone_rendered_when_prefix_set(self):
        """When phone_prefix is set, phone should serialize as an object."""
        user = User.objects.create_user(
            email="hasphone@example.com",
            full_name="Has Phone",
            phone_prefix="+1",
            phone="5551234567",
        )
        data = UserSerializer(user).data
        assert data["phone"]["prefix"] == "+1"
        assert data["phone"]["number"] == "5551234567"

    def test_registration_method_in_response(self):
        user = User.objects.create_user(email="reg@example.com", full_name="Reg User")
        data = UserSerializer(user).data
        assert data["registration_method"] == "email"

    def test_registration_method_oauth(self):
        user = User.objects.create_user(
            email="oauth@example.com",
            full_name="OAuth User",
            registration_method="google",
        )
        data = UserSerializer(user).data
        assert data["registration_method"] == "google"

    def test_linked_providers_empty(self):
        user = User.objects.create_user(email="noprov@example.com", full_name="No Prov")
        data = UserSerializer(user).data
        assert data["linked_providers"] == []

    def test_linked_providers_with_accounts(self):
        user = User.objects.create_user(email="linked@example.com", full_name="Linked User")
        SocialAccount.objects.create(user=user, provider="google", provider_user_id="g1")
        SocialAccount.objects.create(user=user, provider="github", provider_user_id="gh1")
        data = UserSerializer(user).data
        assert sorted(data["linked_providers"]) == ["github", "google"]


class TestUpdateUserSerializer:
    def test_valid_partial_update(self):
        ser = UpdateUserSerializer(data={"full_name": "New Name"})
        assert ser.is_valid(), ser.errors

    def test_valid_all_fields(self):
        ser = UpdateUserSerializer(
            data={
                "full_name": "Full Name",
                "preferred_locale": "en",
                "preferred_currency": "usd",
            }
        )
        assert ser.is_valid(), ser.errors

    def test_empty_data_is_valid(self):
        ser = UpdateUserSerializer(data={})
        assert ser.is_valid(), ser.errors

    def test_null_full_name_is_rejected(self):
        ser = UpdateUserSerializer(data={"full_name": None})
        assert not ser.is_valid()
        assert "full_name" in ser.errors

    def test_avatar_url_is_not_writable(self):
        """avatar_url must only be updated via AvatarView (POST/DELETE)."""
        ser = UpdateUserSerializer(data={"avatar_url": "javascript:alert(1)"})
        assert ser.is_valid(), ser.errors
        assert "avatar_url" not in ser.validated_data

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

    def test_full_name_min_length(self):
        ser = UpdateUserSerializer(data={"full_name": "ab"})
        assert not ser.is_valid()
        assert "full_name" in ser.errors

    def test_phone_valid(self):
        ser = UpdateUserSerializer(data={"phone": {"prefix": "+1", "number": "5551234567"}})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["phone_prefix"] == "+1"
        assert ser.validated_data["phone"] == "5551234567"

    def test_phone_unsupported_prefix_rejected(self):
        ser = UpdateUserSerializer(data={"phone": {"prefix": "+9999", "number": "123"}})
        assert not ser.is_valid()
        assert "phone" in ser.errors

    def test_phone_missing_number_rejected(self):
        ser = UpdateUserSerializer(data={"phone": {"prefix": "+1"}})
        assert not ser.is_valid()
        assert "phone" in ser.errors

    def test_phone_null_clears_phone_fields(self):
        ser = UpdateUserSerializer(data={"phone": None})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["phone_prefix"] is None
        assert ser.validated_data["phone"] is None

    def test_timezone_accepted(self):
        ser = UpdateUserSerializer(data={"timezone": "Europe/Madrid"})
        assert ser.is_valid(), ser.errors

    def test_timezone_null_accepted(self):
        ser = UpdateUserSerializer(data={"timezone": None})
        assert ser.is_valid(), ser.errors

    def test_job_title_accepted(self):
        ser = UpdateUserSerializer(data={"job_title": "Engineer"})
        assert ser.is_valid(), ser.errors

    def test_job_title_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"job_title": "x" * 101})
        assert not ser.is_valid()
        assert "job_title" in ser.errors

    def test_pronouns_accepted(self):
        ser = UpdateUserSerializer(data={"pronouns": "they/them"})
        assert ser.is_valid(), ser.errors

    def test_pronouns_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"pronouns": "x" * 51})
        assert not ser.is_valid()
        assert "pronouns" in ser.errors

    def test_bio_accepted(self):
        ser = UpdateUserSerializer(data={"bio": "A long bio about me."})
        assert ser.is_valid(), ser.errors

    def test_bio_null_accepted(self):
        ser = UpdateUserSerializer(data={"bio": None})
        assert ser.is_valid(), ser.errors

    def test_bio_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"bio": "x" * 501})
        assert not ser.is_valid()
        assert "bio" in ser.errors

    def test_bio_at_max_length_accepted(self):
        ser = UpdateUserSerializer(data={"bio": "x" * 500})
        assert ser.is_valid(), ser.errors

    def test_phone_number_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"phone": {"prefix": "+1", "number": "1" * 16}})
        assert not ser.is_valid()
        assert "phone" in ser.errors

    def test_phone_prefix_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"phone": {"prefix": "+12345", "number": "123456"}})
        assert not ser.is_valid()
        assert "phone" in ser.errors

    def test_phone_missing_prefix_rejected(self):
        ser = UpdateUserSerializer(data={"phone": {"number": "5551234567"}})
        assert not ser.is_valid()
        assert "phone" in ser.errors

    def test_timezone_max_length_rejected(self):
        ser = UpdateUserSerializer(data={"timezone": "x" * 51})
        assert not ser.is_valid()
        assert "timezone" in ser.errors

    def test_pronouns_null_accepted(self):
        ser = UpdateUserSerializer(data={"pronouns": None})
        assert ser.is_valid(), ser.errors

    def test_job_title_null_accepted(self):
        ser = UpdateUserSerializer(data={"job_title": None})
        assert ser.is_valid(), ser.errors
