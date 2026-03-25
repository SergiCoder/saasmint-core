"""URL patterns for the billing app."""

from django.urls import path

from apps.billing.views import (
    ApplyPromoCodeView,
    CancelSubscriptionView,
    ChangePlanView,
    CheckoutView,
    PlanListView,
    PortalView,
    SubscriptionView,
    UpdateSeatCountView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="billing-plans"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("portal/", PortalView.as_view(), name="billing-portal"),
    path("subscription/", SubscriptionView.as_view(), name="billing-subscription"),
    path("subscription/cancel/", CancelSubscriptionView.as_view(), name="billing-cancel"),
    path("subscription/change-plan/", ChangePlanView.as_view(), name="billing-change-plan"),
    path("subscription/promo/", ApplyPromoCodeView.as_view(), name="billing-promo"),
    path("subscription/seats/", UpdateSeatCountView.as_view(), name="billing-seats"),
]
