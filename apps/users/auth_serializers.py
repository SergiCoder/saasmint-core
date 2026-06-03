"""Serializers for authentication endpoints."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.users.captcha import verify_recaptcha
from apps.users.models import FULL_NAME_MIN_LENGTH, User


def _run_password_validators(password: str, user: User | None = None) -> str:
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages)) from exc
    return password


class CaptchaProtectedSerializer(serializers.Serializer[User]):
    """Mixin adding a ``captcha_token`` field + reCAPTCHA v3 verification.

    Subclasses set ``captcha_action`` to the expected v3 action name. The token
    is ``required=False`` so a *missing* token still routes through
    ``verify_recaptcha`` and surfaces as ``code="captcha_failed"`` rather than a
    DRF "field required" error. Verification is a no-op when
    ``RECAPTCHA_SECRET_KEY`` is unset, so dev/tests run without keys.
    """

    captcha_action: ClassVar[str]
    captcha_token = serializers.CharField(write_only=True, required=False, default="")

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        verify_recaptcha(str(attrs.get("captcha_token", "")), action=self.captcha_action)
        return attrs


class RegisterSerializer(CaptchaProtectedSerializer):
    """Request body for creating a new account."""

    captcha_action = "register"

    email = serializers.EmailField()
    password = serializers.CharField(min_length=10, max_length=128, write_only=True)
    full_name = serializers.CharField(min_length=FULL_NAME_MIN_LENGTH, max_length=255)

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
    # Optional: invitee accounts (created via ``accept_invitation``) start
    # with an unusable password. They MUST supply one here so the same
    # request that proves mailbox control also binds a credential the
    # invitation-token interceptor cannot have chosen.
    password = serializers.CharField(
        min_length=10,
        max_length=128,
        write_only=True,
        required=False,
    )

    def validate_password(self, value: str) -> str:
        return _run_password_validators(value)


class ForgotPasswordSerializer(CaptchaProtectedSerializer):
    """Request body for requesting a password-reset email."""

    captcha_action = "forgot_password"

    email = serializers.EmailField()


class ResendVerificationSerializer(CaptchaProtectedSerializer):
    """Request body for re-sending the email-verification message."""

    captcha_action = "resend_verification"

    email = serializers.EmailField()


class OAuthConfirmLinkSerializer(serializers.Serializer[User]):
    token = serializers.CharField()


class ResetPasswordSerializer(serializers.Serializer[User]):
    token = serializers.CharField()
    password = serializers.CharField(min_length=10, max_length=128, write_only=True)

    def validate_password(self, value: str) -> str:
        return _run_password_validators(value)


class ChangePasswordSerializer(serializers.Serializer[User]):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(min_length=10, max_length=128, write_only=True)

    def validate_new_password(self, value: str) -> str:
        return _run_password_validators(value)


class TokenResponseSerializer(serializers.Serializer[User]):
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")
    expires_in = serializers.IntegerField(help_text="Access token lifetime in seconds.")


class MessageResponseSerializer(serializers.Serializer[object]):
    """Schema-only serializer for ``{detail, code}`` envelope responses."""

    detail = serializers.CharField(help_text="Human-readable message.")
    code = serializers.CharField(help_text="Machine-readable code.")
