"""URL patterns for the billing app."""

from django.urls import path

from apps.billing.views import (
    CheckoutSessionView,
    CreditBalanceView,
    PlanListView,
    PortalSessionView,
    ProductCheckoutSessionView,
    ProductListView,
    SubscriptionView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="billing-plans"),
    path("products/", ProductListView.as_view(), name="billing-products"),
    path("checkout-sessions/", CheckoutSessionView.as_view(), name="billing-checkout"),
    path(
        "product-checkout-sessions/",
        ProductCheckoutSessionView.as_view(),
        name="billing-product-checkout",
    ),
    path("portal-sessions/", PortalSessionView.as_view(), name="billing-portal"),
    path("subscriptions/me/", SubscriptionView.as_view(), name="billing-subscription"),
    path("credits/me/", CreditBalanceView.as_view(), name="billing-credits"),
]
