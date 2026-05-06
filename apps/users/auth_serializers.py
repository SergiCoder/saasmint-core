"""Serializers for authentication endpoints."""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.users.models import User


def _run_password_validators(password: str, user: User | None = None) -> str:
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages)) from exc
    return password


class RegisterSerializer(serializers.Serializer[User]):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, max_length=128, write_only=True)
    full_name = serializers.CharField(min_length=3, max_length=255)

    def validate_password(self, value: str) -> str:
        return _run_password_validators(value)


class LoginSerializer(serializers.Serializer[User]):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class RefreshSerializer(serializers.Serializer[User]):
    refresh_token = serializers.CharField()


class LogoutSerializer(serializers.Serializer[User]):
    refresh_token = serializers.CharField()


class VerifyEmailSerializer(serializers.Serializer[User]):
    token = serializers.CharField()


class ForgotPasswordSerializer(serializers.Serializer[User]):
    email = serializers.EmailField()


class ResendVerificationSerializer(serializers.Serializer[User]):
    email = serializers.EmailField()


class OAuthConfirmLinkSerializer(serializers.Serializer[User]):
    token = serializers.CharField()


class ResetPasswordSerializer(serializers.Serializer[User]):
    token = serializers.CharField()
    password = serializers.CharField(min_length=8, max_length=128, write_only=True)

    def validate_password(self, value: str) -> str:
        return _run_password_validators(value)


class ChangePasswordSerializer(serializers.Serializer[User]):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(min_length=8, max_length=128, write_only=True)

    def validate_new_password(self, value: str) -> str:
        return _run_password_validators(value)


class TokenResponseSerializer(serializers.Serializer[User]):
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")


class OAuthExchangeResponseSerializer(TokenResponseSerializer):
    expires_in = serializers.IntegerField(help_text="Access token lifetime in seconds.")


class MessageResponseSerializer(serializers.Serializer[object]):
    """Schema-only serializer for ``{detail, code}`` envelope responses."""

    detail = serializers.CharField(help_text="Human-readable message.")
    code = serializers.CharField(help_text="Machine-readable code.")
