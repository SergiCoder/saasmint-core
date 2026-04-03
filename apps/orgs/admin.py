"""Admin registration for the orgs app."""

from django.contrib import admin

from apps.orgs.models import Org, OrgMember


@admin.register(Org)
class OrgAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("name", "slug", "created_by", "created_at", "deleted_at")
    list_filter = ("deleted_at",)
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at")
    list_select_related = ("created_by",)


@admin.register(OrgMember)
class OrgMemberAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("org", "user", "role", "is_billing", "joined_at")
    list_filter = ("role", "is_billing")
    search_fields = ("org__name", "user__email")
    readonly_fields = ("id", "joined_at")
    list_select_related = ("org", "user")
