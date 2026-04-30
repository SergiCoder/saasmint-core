"""Request/response serializers for the users app."""

from __future__ import annotations

import functools
from typing import Any
from zoneinfo import available_timezones

from rest_framework import serializers

from apps.users.models import User


@functools.cache
def _available_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


class _PhoneReadSerializer(serializers.Serializer[User]):
    prefix = serializers.CharField(source="phone_prefix")
    number = serializers.CharField(source="phone")


class UserSerializer(serializers.ModelSerializer[User]):
    phone = _PhoneReadSerializer(source="*", read_only=True, allow_null=True)
    linked_providers = serializers.SerializerMethodField()
    has_stripe_customer = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "full_name",
            "avatar_url",
            "preferred_locale",
            "preferred_currency",
            "phone",
            "timezone",
            "job_title",
            "pronouns",
            "bio",
            "is_verified",
            "registration_method",
            "linked_providers",
            "has_stripe_customer",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_linked_providers(self, obj: User) -> list[str]:
        prefetched = getattr(obj, "_prefetched_objects_cache", {})
        if "social_accounts" in prefetched:
            return [sa.provider for sa in obj.social_accounts.all()]
        return list(obj.social_accounts.values_list("provider", flat=True))

    def get_has_stripe_customer(self, obj: User) -> bool:
        """Whether the user has a personal Stripe customer.

        The user's billing currency is locked at first purchase on this
        customer (rule 12). After that, even cancellation leaves the
        customer row in place, so the lock survives — frontend uses this
        flag to gate the "your billing currency can't be changed" notice
        on the profile form. Org-scoped Stripe customers don't count;
        the user's currency is independent of any org's billing currency
        (rule 3 — distinct customers per scope).

        Performance: a single indexed ``EXISTS`` on the unique ``user_id``
        (OneToOneField) — ``SELECT 1 ... LIMIT 1``. The flag must reflect
        live DB state (the row can be hard-deleted out from under the
        cached ``User`` instance), so we deliberately do not short-circuit
        via ``_state.fields_cache``.
        """
        from apps.billing.models import StripeCustomer

        return StripeCustomer.objects.filter(user_id=obj.id).exists()

    def to_representation(self, instance: User) -> dict[str, Any]:
        data = super().to_representation(instance)
        if instance.phone_prefix is None and instance.phone is None:
            data["phone"] = None
        return data


class _PhoneWriteSerializer(serializers.Serializer[User]):
    prefix = serializers.CharField(max_length=5, required=True)
    number = serializers.CharField(min_length=4, max_length=15, required=True)

    def validate_prefix(self, value: str) -> str:
        from saasmint_core.services.phone import SUPPORTED_PHONE_PREFIXES

        if value not in SUPPORTED_PHONE_PREFIXES:
            raise serializers.ValidationError(
                f"Unsupported phone prefix. Must be one of: "
                f"{', '.join(sorted(SUPPORTED_PHONE_PREFIXES))}"
            )
        return value


class UpdateUserSerializer(serializers.Serializer[User]):
    full_name = serializers.CharField(min_length=3, max_length=255, required=False)
    preferred_locale = serializers.CharField(max_length=10, required=False)
    preferred_currency = serializers.CharField(max_length=3, required=False)
    phone = _PhoneWriteSerializer(required=False, allow_null=True)
    timezone = serializers.CharField(max_length=50, required=False, allow_null=True)
    job_title = serializers.CharField(max_length=100, required=False, allow_null=True)
    pronouns = serializers.CharField(max_length=50, required=False, allow_null=True)
    bio = serializers.CharField(max_length=500, required=False, allow_null=True)

    def validate_preferred_locale(self, value: str) -> str:
        from saasmint_core.services.locale import SUPPORTED_LOCALES

        return self._validate_in_set(value, SUPPORTED_LOCALES, "locale")

    def validate_preferred_currency(self, value: str) -> str:
        from saasmint_core.services.currency import SUPPORTED_CURRENCIES

        return self._validate_in_set(value, SUPPORTED_CURRENCIES, "currency")

    def validate_timezone(self, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in _available_timezones():
            raise serializers.ValidationError(
                "Unsupported timezone. Must be a valid IANA timezone identifier."
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        phone = attrs.pop("phone", None)
        if phone is not None:
            attrs["phone_prefix"] = phone["prefix"]
            attrs["phone"] = phone["number"]
        elif "phone" in self.initial_data and self.initial_data["phone"] is None:
            attrs["phone_prefix"] = None
            attrs["phone"] = None
        return attrs

    @staticmethod
    def _validate_in_set(value: str, allowed: frozenset[str], label: str) -> str:
        if value not in allowed:
            raise serializers.ValidationError(
                f"Unsupported {label}. Must be one of: {', '.join(sorted(allowed))}"
            )
        return value
