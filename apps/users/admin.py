"""Admin registration for the users app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import available_timezones

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm
from django.http import HttpRequest
from django.utils.safestring import SafeString, mark_safe
from saasmint_core.services.currency import SUPPORTED_CURRENCIES
from saasmint_core.services.locale import SUPPORTED_LOCALES
from saasmint_core.services.phone import SUPPORTED_PHONE_PREFIXES, sort_prefix_key

from apps.users.models import SocialAccount, User

_LOCALE_CHOICES = [("", "---------")] + [(v, v) for v in sorted(SUPPORTED_LOCALES)]
_CURRENCY_CHOICES = [("", "---------")] + [(v, v.upper()) for v in sorted(SUPPORTED_CURRENCIES)]


_PHONE_PREFIX_CHOICES = [("", "---------")] + [
    (k, f"{v} {k}") for k, v in sorted(SUPPORTED_PHONE_PREFIXES.items(), key=sort_prefix_key)
]
_TIMEZONE_CHOICES = [("", "---------")] + [(v, v) for v in sorted(available_timezones())]


class _PasswordWidget(forms.Widget):
    """Show 'Password set' with a reset link instead of the hash breakdown."""

    def render(
        self,
        name: str,
        value: Any,  # noqa: ANN401
        attrs: dict[str, Any] | None = None,
        renderer: Any = None,  # noqa: ANN401
    ) -> SafeString:
        return mark_safe(
            '<a href="../password/" class="button" style="text-decoration:none">Reset password</a>'
        )


class UserChangeForm(BaseUserChangeForm):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        password_field = self.fields.get("password")
        if password_field:
            password_field.widget = _PasswordWidget()
            password_field.help_text = ""

    class Meta:
        model = User
        fields = (
            "email",
            "password",
            "full_name",
            "avatar_url",
            "account_type",
            "registration_method",
            "preferred_locale",
            "preferred_currency",
            "phone_prefix",
            "phone",
            "timezone",
            "job_title",
            "pronouns",
            "bio",
            "is_verified",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
            "deleted_at",
            "scheduled_deletion_at",
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
class UserAdmin(BaseUserAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    form = UserChangeForm

    class Media:
        css: ClassVar[dict[str, tuple[str, ...]]] = {"all": ("users_admin.css",)}

    list_display = ("email", "full_name", "account_type", "is_verified", "is_active", "created_at")
    list_filter = ("account_type", "registration_method", "is_active", "is_staff", "is_verified")
    search_fields = ("email", "full_name")
    ordering = ("-created_at",)
    readonly_fields = (
        "id",
        "registration_method",
        "created_at",
        "deleted_at",
        "scheduled_deletion_at",
    )

    def get_fieldsets(
        self,
        request: HttpRequest,
        obj: Any = None,  # noqa: ANN401
    ) -> _FieldsetSpec:
        return list(super().get_fieldsets(request, obj))

    fieldsets = (
        (None, {"fields": ("id", "email", "password")}),
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
                    "pronouns",
                    "bio",
                    "is_verified",
                    "registration_method",
                )
            },
        ),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Timestamps", {"fields": ("created_at", "deleted_at", "scheduled_deletion_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "password1", "password2"),
            },
        ),
    )


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = ("user", "provider", "provider_user_id", "created_at")
    list_filter = ("provider",)
    search_fields = ("user__email", "provider_user_id")
    readonly_fields = ("id", "created_at")
    raw_id_fields = ("user",)
