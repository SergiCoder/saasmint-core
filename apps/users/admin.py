"""Admin registration for the users app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpRequest

from apps.users.models import User

if TYPE_CHECKING:
    from django.contrib.admin.options import _FieldsetSpec


@admin.register(User)
class UserAdmin(BaseUserAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic but BaseUserAdmin doesn't declare its type parameter
    list_display = ("email", "full_name", "account_type", "is_verified", "is_active", "created_at")
    list_filter = ("account_type", "is_active", "is_staff", "is_verified")
    search_fields = ("email", "full_name", "supabase_uid")
    ordering = ("-created_at",)
    readonly_fields = ("id", "supabase_uid", "created_at", "deleted_at")

    def get_fieldsets(
        self,
        request: HttpRequest,
        obj: Any = None,  # noqa: ANN401
    ) -> _FieldsetSpec:
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj and not obj.is_staff:
            # Hide password field for non-staff (Supabase-only) users
            fieldsets = [
                (name, {**opts, "fields": tuple(f for f in opts["fields"] if f != "password")})
                for name, opts in fieldsets
            ]
        return fieldsets

    fieldsets = (
        (None, {"fields": ("id", "email", "supabase_uid", "password")}),
        (
            "Profile",
            {
                "fields": (
                    "full_name",
                    "avatar_url",
                    "account_type",
                    "preferred_locale",
                    "preferred_currency",
                    "is_verified",
                )
            },
        ),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Timestamps", {"fields": ("created_at", "deleted_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "supabase_uid"),
            },
        ),
    )
