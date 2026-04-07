"""Admin registration for the billing app."""

from django.contrib import admin
from django.http import HttpRequest

from apps.billing.models import (
    Plan,
    PlanPrice,
    Product,
    ProductPrice,
    StripeCustomer,
    StripeEvent,
    Subscription,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("name", "context", "tier", "interval", "is_active")
    list_filter = ("context", "tier", "interval", "is_active")
    search_fields = ("name",)


@admin.register(PlanPrice)
class PlanPriceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("plan", "amount", "stripe_price_id")
    search_fields = ("stripe_price_id",)
    list_select_related = ("plan",)
    ordering = ("plan__name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("name", "type", "credits", "is_active")
    list_filter = ("type", "is_active")
    search_fields = ("name",)


@admin.register(ProductPrice)
class ProductPriceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("product", "amount", "stripe_price_id")
    search_fields = ("stripe_price_id",)
    list_select_related = ("product",)


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = ("stripe_id", "user", "org", "livemode", "created_at")
    list_filter = ("livemode",)
    search_fields = ("stripe_id",)
    readonly_fields = ("id", "stripe_id", "created_at")
    list_select_related = ("user", "org")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]  # django-stubs ModelAdmin is generic; no type param needed at runtime
    list_display = (
        "stripe_id",
        "owner",
        "stripe_customer",
        "status",
        "plan",
        "quantity",
        "trial_ends_at",
        "current_period_end",
        "canceled_at",
    )
    list_filter = ("status",)
    search_fields = ("stripe_id", "user__email", "stripe_customer__user__email")
    readonly_fields = ("id", "stripe_id", "created_at")
    list_select_related = (
        "user",
        "stripe_customer__user",
        "stripe_customer__org",
        "plan",
    )

    @admin.display(description="Owner")
    def owner(self, obj: Subscription) -> str:
        # Free subs link the user directly; paid subs go through stripe_customer.
        if obj.user_id is not None:
            return f"user: {obj.user}"
        customer = obj.stripe_customer
        if customer is not None:
            if customer.user_id is not None:
                return f"user: {customer.user}"
            if customer.org_id is not None:
                return f"org: {customer.org}"
        return "—"


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
