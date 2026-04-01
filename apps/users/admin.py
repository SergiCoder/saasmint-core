"""Admin registration for the users app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import available_timezones

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpRequest
from saasmint_core.services.currency import SUPPORTED_CURRENCIES
from saasmint_core.services.locale import SUPPORTED_LOCALES
from saasmint_core.services.phone import SUPPORTED_PHONE_PREFIXES

from apps.users.models import User

_LOCALE_CHOICES = [("", "---------")] + [(v, v) for v in sorted(SUPPORTED_LOCALES)]
_CURRENCY_CHOICES = [("", "---------")] + [(v, v.upper()) for v in sorted(SUPPORTED_CURRENCIES)]


def _sort_prefixes(x: tuple[str, str]) -> int:
    return int(x[0].lstrip("+"))


_PHONE_PREFIX_CHOICES = [("", "---------")] + [
    (k, f"{v} {k}") for k, v in sorted(SUPPORTED_PHONE_PREFIXES.items(), key=_sort_prefixes)
]
_TIMEZONE_CHOICES = [("", "---------")] + [(v, v) for v in sorted(available_timezones())]


class UserChangeForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = User
        fields = (
            "email",
            "full_name",
            "avatar_url",
            "account_type",
            "preferred_locale",
            "preferred_currency",
            "phone_prefix",
            "phone",
            "timezone",
            "job_title",
            "bio",
            "is_verified",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
            "deleted_at",
        )
        labels: ClassVar[dict[str, str]] = {
            "phone_prefix": "Phone",
            "phone": "",
        }
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "preferred_locale": forms.Select(choices=_LOCALE_CHOICES),
            "preferred_currency": forms.Select(choices=_CURRENCY_CHOICES),
            "phone_prefix": forms.Select(choices=_PHONE_PREFIX_CHOICES),
            "timezone": forms.Select(choices=_TIMEZONE_CHOICES),
        }


if TYPE_CHECKING:
    from django.contrib.admin.options import _FieldsetSpec


@admin.register(User)
class UserAdmin(BaseUserAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic but BaseUserAdmin doesn't declare its type parameter
    form = UserChangeForm

    class Media:
        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("users_admin.css",)}

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
                    ("phone_prefix", "phone"),
                    "timezone",
                    "job_title",
                    "bio",
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
