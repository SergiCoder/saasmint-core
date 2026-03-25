"""Extended Django admin — subscription status on user list, Stripe event log, impersonation."""

from django.contrib import admin
from django.db.models import OuterRef, QuerySet, Subquery
from django.http import HttpRequest
from django.utils.html import format_html

from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, Subscription
from apps.users.models import User

admin.site.site_url = "/dashboard/"

# Re-register User admin to show subscription status
admin.site.unregister(User)


@admin.register(User)
class UserAdminExtended(admin.ModelAdmin):
    list_display = (
        "email",
        "full_name",
        "account_type",
        "subscription_status",
        "is_verified",
        "is_active",
        "created_at",
    )
    list_filter = ("account_type", "is_active", "is_staff", "is_verified")
    search_fields = ("email", "full_name", "supabase_uid")
    ordering = ("-created_at",)
    readonly_fields = ("id", "supabase_uid", "created_at", "deleted_at")

    def get_queryset(self, request: HttpRequest) -> QuerySet[User]:  # type: ignore[override]
        qs = super().get_queryset(request)
        customer_sub = Subscription.objects.filter(
            stripe_customer__user=OuterRef("pk"),
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        ).order_by("-created_at")
        return qs.annotate(_subscription_status=Subquery(customer_sub.values("status")[:1]))

    @admin.display(description="Subscription")
    def subscription_status(self, obj: User) -> str:
        status = getattr(obj, "_subscription_status", None)
        if not status:
            return "—"
        colour = {"active": "green", "trialing": "blue", "past_due": "orange"}.get(status, "grey")
        return format_html('<span style="color:{}">{}</span>', colour, status)
