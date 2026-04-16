"""Dashboard and impersonation landing views."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView
from hijack.views import AcquireUserView, ReleaseUserView
from saasmint_core.domain.subscription import PlanContext
from saasmint_core.domain.user import AccountType

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


@method_decorator(staff_member_required, name="dispatch")
@method_decorator(require_POST, name="dispatch")
class HijackAcquireView(AcquireUserView):
    """Override hijack acquire to always land on the dashboard.

    `staff_member_required` forces staff login before the hijack machinery
    checks HIJACK_PERMISSION_CHECK (superusers only). `require_POST` ensures
    the view never services a GET, which is never a valid hijack trigger
    (hijack's base view already POSTs, but we enforce it at the URL layer
    since the endpoint is mounted outside `/admin/`).
    """

    def get_success_url(self) -> str:
        return reverse("dashboard:dashboard")


@method_decorator(staff_member_required, name="dispatch")
@method_decorator(require_POST, name="dispatch")
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
        # Independent fetches — run concurrently to cut round-trip latency.
        plan_context = (
            PlanContext.TEAM
            if user.account_type == AccountType.ORG_MEMBER
            else PlanContext.PERSONAL
        )
        subscription, plans, products, org_memberships = await asyncio.gather(
            DjangoSubscriptionRepository().get_active_for_user(user.id),
            DjangoPlanRepository().list_active_by_context(plan_context),
            DjangoProductRepository().list_active(),
            _get_org_memberships(user),
        )
        # Look up the subscription's plan from the already-fetched list to avoid
        # an extra DB round-trip.
        plan = (
            next(
                (p for p in plans if p.id == subscription.plan_id),
                None,
            )
            if subscription is not None
            else None
        )
        ctx = self.get_context_data(
            subscription=subscription,
            plan=plan,
            plans=plans,
            products=products,
            org_memberships=org_memberships,
            **kwargs,
        )
        return self.render_to_response(ctx)
