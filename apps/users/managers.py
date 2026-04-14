from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import BaseUserManager

if TYPE_CHECKING:
    from apps.users.models import User


class UserManager(BaseUserManager["User"]):
    def create_user(
        self,
        email: str,
        password: str | None = None,
        **extra: Any,  # noqa: ANN401  # extra kwargs passed to model constructor; heterogeneous by design
    ) -> User:
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user: User = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        password: str | None = None,
        **extra: Any,  # noqa: ANN401  # extra kwargs passed to model constructor; heterogeneous by design
    ) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        extra.setdefault("full_name", "Admin")
        return self.create_user(email, password, **extra)
