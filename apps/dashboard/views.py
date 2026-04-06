"""Dashboard and impersonation landing views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.generic import TemplateView
from hijack.views import AcquireUserView, ReleaseUserView

from apps.billing.repositories import (
    DjangoPlanRepository,
    DjangoProductRepository,
    DjangoSubscriptionRepository,
)
from apps.orgs.models import OrgMember

if TYPE_CHECKING:
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


class DashboardView(TemplateView):
    """GET /dashboard/ — render account, subscription, and org membership summary."""

    template_name = "dashboard/dashboard.html"

    async def get(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        user = await request.auser()
        if not user.is_authenticated:
            return HttpResponseRedirect(f"{settings.LOGIN_URL}?next={request.path}")
        subscription = await DjangoSubscriptionRepository().get_active_for_user(user.id)
        plan = (
            await DjangoPlanRepository().get_by_id(subscription.plan_id)
            if subscription is not None
            else None
        )
        plans = await DjangoPlanRepository().list_active()
        products = await DjangoProductRepository().list_active()
        org_memberships = await _get_org_memberships(user)
        ctx = self.get_context_data(
            subscription=subscription,
            plan=plan,
            plans=plans,
            products=products,
            org_memberships=org_memberships,
            **kwargs,
        )
        return self.render_to_response(ctx)
