"""Extended Django admin — re-registers User with subscription status column and sets site_url to /dashboard/."""  # noqa: E501

from django.contrib import admin
from django.db.models import OuterRef, QuerySet, Subquery
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, Subscription, SubscriptionStatus
from apps.users.admin import UserAdmin
from apps.users.models import User

admin.site.site_url = "/dashboard/"

# Re-register User admin to show subscription status
admin.site.unregister(User)


@admin.register(User)
class UserAdminExtended(UserAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic but BaseUserAdmin doesn't declare its type parameter
    list_display = (
        "email",
        "full_name",
        "account_type",
        "subscription_status",
        "is_verified",
        "is_active",
        "created_at",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[User]:
        qs = super().get_queryset(request)  # type: ignore[misc]  # django-stubs types get_queryset as returning QuerySet[Any]; we narrow to QuerySet[User]
        customer_sub = Subscription.objects.filter(
            stripe_customer__user=OuterRef("pk"),
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        ).order_by("-created_at")
        return qs.annotate(_subscription_status=Subquery(customer_sub.values("status")[:1]))

    @admin.display(description="Subscription")
    def subscription_status(self, obj: User) -> str | SafeString:
        status = getattr(obj, "_subscription_status", None)
        if not status:
            return "—"
        colour = {
            SubscriptionStatus.ACTIVE: "green",
            SubscriptionStatus.TRIALING: "blue",
            SubscriptionStatus.PAST_DUE: "orange",
        }.get(status, "grey")
        return format_html('<span style="color:{}">{}</span>', colour, status)
