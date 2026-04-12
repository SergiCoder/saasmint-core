"""Admin registration for the orgs app."""

from django.contrib import admin

from apps.orgs.models import Invitation, Org, OrgMember


@admin.register(Org)
class OrgAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = ("name", "slug", "created_by", "created_at", "deleted_at")
    list_filter = ("deleted_at",)
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at")
    list_select_related = ("created_by",)


@admin.register(OrgMember)
class OrgMemberAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = ("org", "user", "role", "is_billing", "joined_at")
    list_filter = ("role", "is_billing")
    search_fields = ("org__name", "user__email")
    readonly_fields = ("id", "joined_at")
    list_select_related = ("org", "user")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = ("email", "org", "role", "status", "invited_by", "created_at", "expires_at")
    list_filter = ("status", "role")
    search_fields = ("email", "org__name")
    readonly_fields = ("id", "token", "created_at")
    list_select_related = ("org", "invited_by")
