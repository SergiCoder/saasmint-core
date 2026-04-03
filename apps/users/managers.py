from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import BaseUserManager

if TYPE_CHECKING:
    from apps.users.models import User


class UserManager(BaseUserManager["User"]):
    def create_user(
        self,
        email: str,
        supabase_uid: str,
        **extra: Any,  # noqa: ANN401  # extra kwargs passed to model constructor; heterogeneous by design
    ) -> User:
        email = self.normalize_email(email)
        user: User = self.model(email=email, supabase_uid=supabase_uid, **extra)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        supabase_uid: str = "superuser",
        **extra: Any,  # noqa: ANN401  # extra kwargs passed to model constructor; heterogeneous by design
    ) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self.create_user(email, supabase_uid, **extra)
