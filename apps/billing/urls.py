"""URL patterns for the billing app."""

from django.urls import path

from apps.billing.views import (
    ApplyPromoCodeView,
    CheckoutSessionView,
    PlanListView,
    PortalSessionView,
    ProductListView,
    SubscriptionView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="billing-plans"),
    path("products/", ProductListView.as_view(), name="billing-products"),
    path("checkout-sessions/", CheckoutSessionView.as_view(), name="billing-checkout"),
    path("portal-sessions/", PortalSessionView.as_view(), name="billing-portal"),
    path("subscription/", SubscriptionView.as_view(), name="billing-subscription"),
    path("subscription/promo-code/", ApplyPromoCodeView.as_view(), name="billing-promo"),
]
