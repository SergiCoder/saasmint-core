"""Dashboard and impersonation landing views."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from hijack.views import AcquireUserView, ReleaseUserView

from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer, Subscription
from apps.orgs.models import OrgMember
from apps.users.models import User


class HijackAcquireView(AcquireUserView):  # type: ignore[misc]
    """Override hijack acquire to always land on the dashboard."""

    def get_success_url(self) -> str:
        return str(reverse_lazy("dashboard:dashboard"))


class HijackReleaseView(ReleaseUserView):  # type: ignore[misc]
    """Override hijack release to redirect to admin users list."""

    def get_success_url(self) -> str:
        return str(reverse_lazy("admin:users_user_changelist"))


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["subscription"] = _active_subscription(user)
        ctx["org_memberships"] = OrgMember.objects.filter(user=user).select_related("org")  # type: ignore[misc]
        return ctx


def _active_subscription(user: User) -> Subscription | None:
    customer = StripeCustomer.objects.filter(user=user).first()
    if customer is None:
        return None
    return (  # type: ignore[no-any-return]
        customer.subscriptions.select_related("plan")
        .filter(status__in=ACTIVE_SUBSCRIPTION_STATUSES)
        .order_by("-created_at")
        .first()
    )
