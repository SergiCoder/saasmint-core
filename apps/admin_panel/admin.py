"""Extended Django admin.

Re-registers User with subscription status column and sets site_url to /dashboard/.
"""

from typing import ClassVar

from django.contrib import admin
from django.db.models import OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
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
class UserAdminExtended(UserAdmin):  # type: ignore[type-arg]  # django-stubs generic; not subscriptable at runtime
    list_display = (
        "email",
        "full_name",
        "account_type",
        "subscription_status",
        "is_verified",
        "is_active",
        "created_at",
    )
    list_filter: ClassVar[tuple[object, ...]] = (
        "account_type",
        "registration_method",
        "is_active",
        "is_staff",
        "is_verified",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[User]:
        qs = super().get_queryset(request)  # type: ignore[misc]  # django-stubs returns QuerySet[Any]; narrowing to QuerySet[User]
        # Two separate subqueries so each one hits a single-column predicate
        # and can use its partial index (idx_sub_user_status / idx_sub_customer_status).
        # The OR'd form defeats both indexes and turns into a seq scan per row.
        active = Subscription.objects.filter(status__in=ACTIVE_SUBSCRIPTION_STATUSES).order_by(
            "-created_at"
        )
        by_user = active.filter(user_id=OuterRef("pk")).values("status")[:1]
        by_customer = active.filter(stripe_customer__user_id=OuterRef("pk")).values("status")[:1]
        return qs.annotate(
            _subscription_status=Coalesce(Subquery(by_user), Subquery(by_customer))
        )

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
