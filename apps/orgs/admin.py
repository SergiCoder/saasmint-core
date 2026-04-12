"""Admin registration for the orgs app."""

import logging

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.orgs.models import Invitation, Org, OrgMember

logger = logging.getLogger(__name__)


@admin.register(Org)
class OrgAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = ("name", "slug", "created_by", "created_at", "deleted_at")
    list_filter = ("deleted_at",)
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at")
    list_select_related = ("created_by",)
    actions = ["delete_org_action"]  # noqa: RUF012

    @admin.action(description="Delete selected orgs (cancel subs, hard-delete members)")
    def delete_org_action(self, request: HttpRequest, queryset: QuerySet[Org]) -> None:
        from apps.orgs.services import delete_org

        count = 0
        for org in queryset.filter(deleted_at__isnull=True):
            delete_org(org)
            count += 1
            logger.info("Admin %s deleted org %s (%s)", request.user, org.slug, org.id)

        self.message_user(request, f"Deleted {count} org(s) and all associated member accounts.")


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
