"""Django ORM model for the application user."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.cache import cache
from django.core.validators import MinLengthValidator
from django.db import models
from django.db.models import Index

from apps.users.managers import UserManager

AUTH_USER_CACHE_KEY = "auth_user:{}"


class AccountType(models.TextChoices):
    PERSONAL = "personal", "Personal"
    ORG_MEMBER = "org_member", "Org Member"


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, validators=[MinLengthValidator(3)])
    avatar_url = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable TextField intentional: NULL means no avatar set (distinguishable from empty string)
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.PERSONAL,
    )
    preferred_locale = models.CharField(max_length=10, default="en")
    preferred_currency = models.CharField(max_length=3, default="usd")
    phone_prefix = models.CharField(max_length=5, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means prefix not set (e.g. "+34")
    phone = models.CharField(max_length=15, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means phone not set
    timezone = models.CharField(max_length=50, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means timezone not set
    job_title = models.CharField(max_length=100, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means job title not set
    pronouns = models.CharField(max_length=50, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means "don't specify"
    bio = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable TextField intentional: NULL means bio not set
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_deletion_at = models.DateTimeField(null=True, blank=True, db_index=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["full_name"]

    objects: UserManager = UserManager()

    class Meta:
        db_table = "users"

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401  # *args/**kwargs forwarded to super().save(); heterogeneous by design
        super().save(*args, **kwargs)
        cache.delete(AUTH_USER_CACHE_KEY.format(self.id))

    def __str__(self) -> str:
        return self.email


class RefreshToken(models.Model):
    """Server-side refresh token supporting revocation and rotation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "refresh_tokens"
        indexes: ClassVar[list[Index]] = [
            models.Index(fields=["user", "-created_at"], name="idx_refresh_user_created"),
        ]

    def __str__(self) -> str:
        return f"RefreshToken({self.id}, user={self.user_id})"

    @property
    def is_valid(self) -> bool:
        from datetime import UTC, datetime

        return self.revoked_at is None and self.expires_at > datetime.now(UTC)


class EmailVerificationToken(models.Model):
    """One-time token sent to verify a user's email address."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_verification_tokens"

    def __str__(self) -> str:
        return f"EmailVerificationToken({self.id}, user={self.user_id})"


class PasswordResetToken(models.Model):
    """One-time token for password reset flow."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "password_reset_tokens"

    def __str__(self) -> str:
        return f"PasswordResetToken({self.id}, user={self.user_id})"
