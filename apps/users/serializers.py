"""Request/response serializers for the users app."""

from __future__ import annotations

from rest_framework import serializers

from apps.users.models import User


class UserSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "full_name",
            "avatar_url",
            "account_type",
            "preferred_locale",
            "preferred_currency",
            "is_verified",
            "created_at",
        )
        read_only_fields = fields


class UpdateUserSerializer(serializers.Serializer[User]):
    full_name = serializers.CharField(max_length=255, required=False, allow_null=True)
    avatar_url = serializers.URLField(required=False, allow_null=True)
    preferred_locale = serializers.CharField(max_length=10, required=False)
    preferred_currency = serializers.CharField(max_length=3, required=False)

    def validate_preferred_locale(self, value: str) -> str:
        from stripe_saas_core.services.locale import SUPPORTED_LOCALES

        if value not in SUPPORTED_LOCALES:
            raise serializers.ValidationError(
                f"Unsupported locale. Must be one of: {', '.join(sorted(SUPPORTED_LOCALES))}"
            )
        return value

    def validate_preferred_currency(self, value: str) -> str:
        from stripe_saas_core.services.currency import SUPPORTED_CURRENCIES

        if value not in SUPPORTED_CURRENCIES:
            raise serializers.ValidationError(
                f"Unsupported currency. Must be one of: {', '.join(sorted(SUPPORTED_CURRENCIES))}"
            )
        return value
