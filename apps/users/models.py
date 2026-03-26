"""Django ORM model for the application user."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.cache import cache
from django.db import models

from apps.users.managers import UserManager

AUTH_USER_CACHE_KEY = "auth_user:{}"


class AccountType(models.TextChoices):
    PERSONAL = "personal", "Personal"
    ORG_MEMBER = "org_member", "Org Member"


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    supabase_uid = models.CharField(max_length=255, unique=True)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True, null=True)  # noqa: DJ001  # nullable CharField intentional: NULL means name not set (distinguishable from empty string)
    avatar_url = models.TextField(blank=True, null=True)  # noqa: DJ001  # nullable TextField intentional: NULL means no avatar set (distinguishable from empty string)
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default=AccountType.PERSONAL,
    )
    preferred_locale = models.CharField(max_length=10, default="en")
    preferred_currency = models.CharField(max_length=3, default="usd")
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["supabase_uid"]

    objects: UserManager = UserManager()

    class Meta:
        db_table = "users"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["supabase_uid"],
                condition=models.Q(deleted_at__isnull=True),
                name="ix_users_supabase_active",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401  # *args/**kwargs forwarded to super().save(); heterogeneous by design
        super().save(*args, **kwargs)
        cache.delete(AUTH_USER_CACHE_KEY.format(self.supabase_uid))

    def __str__(self) -> str:
        return self.email
