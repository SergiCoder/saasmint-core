"""Dashboard and impersonation landing views."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.views.generic import TemplateView
from hijack.views import AcquireUserView, ReleaseUserView

from apps.billing.repositories import DjangoSubscriptionRepository
from apps.orgs.models import OrgMember
from apps.users.models import User


async def _get_org_memberships(user: User) -> list[OrgMember]:
    return [m async for m in OrgMember.objects.filter(user=user).select_related("org")]


class HijackAcquireView(AcquireUserView):
    """Override hijack acquire to always land on the dashboard."""

    def get_success_url(self) -> str:
        return reverse("dashboard:dashboard")


class HijackReleaseView(ReleaseUserView):
    """Override hijack release to redirect to admin users list."""

    def get_success_url(self) -> str:
        return reverse("admin:users_user_changelist")


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    async def get(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        user = request.user
        subscription = await DjangoSubscriptionRepository().get_active_for_user(user.id)
        org_memberships = await _get_org_memberships(user)
        ctx = self.get_context_data(
            subscription=subscription,
            org_memberships=org_memberships,
            **kwargs,
        )
        return self.render_to_response(ctx)
