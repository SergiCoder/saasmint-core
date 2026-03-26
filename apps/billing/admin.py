"""Admin registration for the billing app."""

from django.contrib import admin
from django.http import HttpRequest

from apps.billing.models import Plan, PlanPrice, StripeCustomer, StripeEvent, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("name", "context", "interval", "is_active")
    list_filter = ("context", "interval", "is_active")
    search_fields = ("name",)


@admin.register(PlanPrice)
class PlanPriceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("plan", "currency", "amount", "stripe_price_id")
    list_filter = ("currency",)
    search_fields = ("stripe_price_id",)
    list_select_related = ("plan",)


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("stripe_id", "user", "livemode", "created_at")
    list_filter = ("livemode",)
    search_fields = ("stripe_id",)
    readonly_fields = ("id", "stripe_id", "created_at")
    list_select_related = ("user",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = (
        "stripe_id",
        "stripe_customer",
        "status",
        "plan",
        "quantity",
        "current_period_end",
    )
    list_filter = ("status",)
    search_fields = ("stripe_id",)
    readonly_fields = ("id", "stripe_id", "created_at")
    list_select_related = ("stripe_customer", "plan")


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("stripe_id", "type", "livemode", "processed_at", "error", "created_at")
    list_filter = ("type", "livemode")
    search_fields = ("stripe_id", "type")
    readonly_fields = ("id", "stripe_id", "type", "livemode", "payload", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
