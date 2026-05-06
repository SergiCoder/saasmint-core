"""Django ORM model for the application user."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.cache import cache
from django.core.validators import MinLengthValidator
from django.db import models
from django.db.models import Index
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.users.managers import UserManager

AUTH_USER_CACHE_KEY = "auth_user:{}"


class RegistrationMethod(models.TextChoices):
    EMAIL = "email", "Email"
    GOOGLE = "google", "Google"
    GITHUB = "github", "GitHub"
    MICROSOFT = "microsoft", "Microsoft"


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, validators=[MinLengthValidator(3)])
    avatar_url = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable TextField intentional: NULL means no avatar set (distinguishable from empty string)
    preferred_locale = models.CharField(max_length=10, default="en")
    preferred_currency = models.CharField(max_length=3, default="usd")
    phone_prefix = models.CharField(max_length=5, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means prefix not set (e.g. "+34")
    phone = models.CharField(max_length=15, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means phone not set
    timezone = models.CharField(max_length=50, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means timezone not set
    job_title = models.CharField(max_length=100, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means job title not set
    pronouns = models.CharField(max_length=50, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means "don't specify"
    bio = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable TextField intentional: NULL means bio not set
    is_verified = models.BooleanField(default=False)
    registration_method = models.CharField(
        max_length=20,
        choices=RegistrationMethod.choices,
        default=RegistrationMethod.EMAIL,
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["full_name"]

    objects: UserManager = UserManager()

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return self.email


@receiver(post_save, sender="users.User")
@receiver(post_delete, sender="users.User")
def _invalidate_auth_user_cache(sender: object, instance: User, **kwargs: object) -> None:
    """Clear the cached auth-user snapshot on any User change.

    Using signals instead of a save() override so that bulk ORM updates and
    admin `update_fields` operations also invalidate the cache — the save
    override was silently bypassed by `QuerySet.update()`.
    """
    cache.delete(AUTH_USER_CACHE_KEY.format(instance.id))


class RefreshToken(models.Model):
    """Server-side refresh token supporting revocation and rotation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "refresh_tokens"
        indexes: ClassVar[list[Index]] = [
            models.Index(fields=["user", "-created_at"], name="idx_refresh_user_created"),
            # Live-token lookup: partial index over non-revoked rows keeps the
            # tree tiny since revocations are permanent and accumulate.
            models.Index(
                fields=["user"],
                name="idx_refresh_user_active",
                condition=models.Q(revoked_at__isnull=True),
            ),
            # cleanup_expired_refresh_tokens scans expires_at__lt=now on every
            # daily run — without this the batch-ID subquery falls back to a
            # sequential scan.
            models.Index(fields=["expires_at"], name="idx_refresh_expires_at"),
        ]

    def __str__(self) -> str:
        return f"RefreshToken({self.id}, user={self.user_id})"

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and self.expires_at > datetime.now(UTC)


class _OneTimeToken(models.Model):
    """Abstract base for hashed one-time tokens (email verification, password reset)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Subclasses must declare ``user`` with an explicit ``related_name``.
    user_id: uuid.UUID  # populated by the FK declared in each subclass
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.id}, user={self.user_id})"


class EmailVerificationToken(_OneTimeToken):
    """One-time token sent to verify a user's email address."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_tokens")

    class Meta(_OneTimeToken.Meta):
        db_table = "email_verification_tokens"
        indexes: ClassVar[list[Index]] = [
            # Cleanup task filters expires_at__lt=now; without this index the
            # batch-ID subquery does a sequential scan.
            models.Index(fields=["expires_at"], name="idx_email_verif_expires_at"),
        ]


class SocialAccount(models.Model):
    """Links a User to an OAuth provider account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="social_accounts")
    provider = models.CharField(max_length=20)
    provider_user_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "social_accounts"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["provider", "provider_user_id"],
                name="uq_social_provider_uid",
            ),
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="uq_social_user_provider",
            ),
        ]

    def __str__(self) -> str:
        return f"SocialAccount({self.provider}, user={self.user_id})"


class PasswordResetToken(_OneTimeToken):
    """One-time token for password reset flow."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_reset_tokens")

    class Meta(_OneTimeToken.Meta):
        db_table = "password_reset_tokens"
        indexes: ClassVar[list[Index]] = [
            # Cleanup task filters expires_at__lt=now; without this index the
            # batch-ID subquery does a sequential scan.
            models.Index(fields=["expires_at"], name="idx_pwd_reset_expires_at"),
        ]


class SocialLinkRequest(_OneTimeToken):
    """Pending request to link a new OAuth provider to an existing User.

    Minted on the OAuth callback when the provider's email matches an
    existing account but cannot be auto-linked — either ``email_verified``
    is false, or the provider is not on ``TRUSTED_FOR_AUTO_LINK``. The user
    proves mailbox control by clicking the confirmation link sent to that
    email; the click then attaches the SocialAccount and signs the user in.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="social_link_requests")
    provider = models.CharField(max_length=20)
    provider_user_id = models.CharField(max_length=255)
    full_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable: NULL means provider gave no avatar

    class Meta(_OneTimeToken.Meta):
        db_table = "social_link_requests"
        indexes: ClassVar[list[Index]] = [
            # Cleanup task filters expires_at__lt=now on every daily run;
            # without this index the batch-ID-subquery does a sequential scan.
            models.Index(fields=["expires_at"], name="idx_social_link_expires_at"),
            # OAuth callback invalidates prior pending requests with
            # filter(user=..., used_at__isnull=True) on every collision —
            # partial index keeps the tree tiny since used rows accumulate.
            models.Index(
                fields=["user"],
                name="idx_social_link_user_active",
                condition=models.Q(used_at__isnull=True),
            ),
        ]
