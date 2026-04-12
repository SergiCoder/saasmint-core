"""Admin registration for the orgs app."""

import logging

from django.contrib import admin
from django.db.models import Count, QuerySet
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse

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

    def has_delete_permission(self, request: HttpRequest, obj: Org | None = None) -> bool:
        # Disable built-in delete (detail page button + bulk action) — it bypasses
        # Stripe cancellation and member cleanup. Use the custom action instead.
        return False

    @admin.action(description="Delete selected orgs (cancel subs, hard-delete members)")
    def delete_org_action(
        self, request: HttpRequest, queryset: QuerySet[Org]
    ) -> HttpResponse | None:
        from apps.orgs.services import delete_org

        # Show confirmation page unless already confirmed
        if "confirm" not in request.POST:
            orgs = queryset.select_related("created_by").annotate(
                member_count=Count("members"),
            )
            return TemplateResponse(
                request,
                "admin/orgs/delete_org_confirmation.html",
                {
                    **self.admin_site.each_context(request),
                    "title": "Confirm org deletion",
                    "orgs": orgs,
                    "opts": self.model._meta,
                },
            )

        count = 0
        for org in queryset:
            delete_org(org)
            count += 1
            logger.info("Admin %s deleted org %s (%s)", request.user, org.slug, org.id)

        self.message_user(request, f"Deleted {count} org(s) and all associated member accounts.")
        return None


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
