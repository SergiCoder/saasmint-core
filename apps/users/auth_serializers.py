"""Serializers for authentication endpoints."""

from __future__ import annotations

from rest_framework import serializers

from apps.users.models import User


class RegisterSerializer(serializers.Serializer[User]):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, max_length=128, write_only=True)
    full_name = serializers.CharField(min_length=3, max_length=255)


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


class ResetPasswordSerializer(serializers.Serializer[User]):
    token = serializers.CharField()
    password = serializers.CharField(min_length=8, max_length=128, write_only=True)


class ChangePasswordSerializer(serializers.Serializer[User]):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(min_length=8, max_length=128, write_only=True)


class TokenResponseSerializer(serializers.Serializer[User]):
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")
