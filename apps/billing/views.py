"""Billing API views — checkout, portal, subscriptions."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.subscription import Subscription
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    get_or_create_customer,
)
from saasmint_core.services.subscriptions import (
    apply_promo_code,
    change_plan,
    update_seat_count,
)

from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, PlanContext, PlanPrice
from apps.billing.models import Plan as PlanModel
from apps.billing.models import Subscription as SubscriptionModel
from apps.billing.repositories import (
    DjangoStripeCustomerRepository,
    DjangoSubscriptionRepository,
)
from apps.billing.serializers import (
    CheckoutRequestSerializer,
    PlanSerializer,
    PortalRequestSerializer,
    PromoCodeSerializer,
    SubscriptionSerializer,
    UpdateSubscriptionSerializer,
)
from helpers import get_user

_customer_repo = DjangoStripeCustomerRepository()
_subscription_repo = DjangoSubscriptionRepository()


async def _get_customer_and_subscription(
    user_id: UUID,
) -> tuple[StripeCustomer, Subscription]:
    """Fetch the Stripe customer and active subscription, or raise NotFound."""
    customer = await _customer_repo.get_by_user_id(user_id)
    if customer is None:
        raise NotFound("No Stripe customer found.")
    sub = await _subscription_repo.get_active_for_customer(customer.id)
    if sub is None:
        raise NotFound("No active subscription found.")
    return customer, sub


def _get_active_plan_price(stripe_price_id: str) -> PlanPrice:
    """Validate a plan_price_id exists in DB and belongs to an active plan."""
    plan_price = (
        PlanPrice.objects.select_related("plan")
        .filter(stripe_price_id=stripe_price_id, plan__is_active=True)
        .first()
    )
    if plan_price is None:
        raise NotFound("Invalid plan price.")
    return plan_price


class PlanListView(APIView):
    """GET /api/v1/billing/plans — list active plans with prices."""

    @extend_schema(responses=PlanSerializer(many=True), tags=["billing"])
    def get(self, request: Request) -> Response:
        data = cache.get("active_plans")
        if data is None:
            plans = PlanModel.objects.filter(is_active=True).prefetch_related("prices")
            data = PlanSerializer(plans, many=True).data
            cache.set("active_plans", data, timeout=300)
        return Response(data)


class CheckoutSessionView(APIView):
    """POST /api/v1/billing/checkout-sessions — create a Stripe Checkout Session."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={201: inline_serializer("CheckoutResponse", {"url": drf_serializers.URLField()})},
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = CheckoutRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        plan_price = _get_active_plan_price(data["plan_price_id"])

        # Orgs are not eligible for trial periods
        trial_period_days = data["trial_period_days"]
        if trial_period_days is not None and plan_price.plan.context == PlanContext.TEAM:
            trial_period_days = None

        async def _do() -> str:
            customer = await get_or_create_customer(
                user_id=user.id,
                email=str(user.email),
                name=user.full_name,
                locale=user.preferred_locale,
                customer_repo=_customer_repo,
            )
            return await create_checkout_session(
                stripe_customer_id=customer.stripe_id,
                client_reference_id=str(user.id),
                price_id=data["plan_price_id"],
                quantity=data["quantity"],
                promo_code=data["promo_code"],
                locale=user.preferred_locale,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
                trial_period_days=trial_period_days,
            )

        url = async_to_sync(_do)()
        return Response({"url": url}, status=status.HTTP_201_CREATED)


class PortalSessionView(APIView):
    """POST /api/v1/billing/portal-sessions — create a Stripe Customer Portal session."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(
        request=PortalRequestSerializer,
        responses={201: inline_serializer("PortalResponse", {"url": drf_serializers.URLField()})},
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = PortalRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        async def _do() -> str:
            customer = await get_or_create_customer(
                user_id=user.id,
                email=str(user.email),
                name=user.full_name,
                locale=user.preferred_locale,
                customer_repo=_customer_repo,
            )
            return await create_billing_portal_session(
                stripe_customer_id=customer.stripe_id,
                locale=user.preferred_locale,
                return_url=ser.validated_data["return_url"],
            )

        url = async_to_sync(_do)()
        return Response({"url": url}, status=status.HTTP_201_CREATED)


class SubscriptionView(APIView):
    """GET/PATCH/DELETE /api/v1/billing/subscription — manage the current subscription."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(responses={200: SubscriptionSerializer, 404: None}, tags=["billing"])
    def get(self, request: Request) -> Response:
        user = get_user(request)
        try:
            customer = user.stripe_customer
            sub = (
                SubscriptionModel.objects.select_related("plan")
                .filter(
                    stripe_customer=customer,
                    status__in=ACTIVE_SUBSCRIPTION_STATUSES,
                )
                .latest("created_at")
            )
            return Response(SubscriptionSerializer(sub).data)
        except ObjectDoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

    @extend_schema(request=UpdateSubscriptionSerializer, responses={204: None}, tags=["billing"])
    def patch(self, request: Request) -> Response:
        user = get_user(request)
        ser = UpdateSubscriptionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if "plan_price_id" in data:
            _get_active_plan_price(data["plan_price_id"])

        async def _do() -> None:
            _, sub = await _get_customer_and_subscription(user.id)
            if "plan_price_id" in data:
                await change_plan(
                    stripe_subscription_id=sub.stripe_id,
                    new_stripe_price_id=data["plan_price_id"],
                    prorate=data["prorate"],
                )
            if "quantity" in data:
                await update_seat_count(
                    stripe_subscription_id=sub.stripe_id,
                    quantity=data["quantity"],
                )

        async_to_sync(_do)()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={204: None}, tags=["billing"])
    def delete(self, request: Request) -> Response:
        user = get_user(request)

        async def _do() -> None:
            customer, _ = await _get_customer_and_subscription(user.id)
            await cancel_subscription(
                stripe_customer_id=customer.id,
                at_period_end=True,
                subscription_repo=_subscription_repo,
            )

        async_to_sync(_do)()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApplyPromoCodeView(APIView):
    """POST /api/v1/billing/subscription/promo-code — apply a promo code."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(request=PromoCodeSerializer, responses={200: None}, tags=["billing"])
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = PromoCodeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        async def _do() -> None:
            _, sub = await _get_customer_and_subscription(user.id)
            await apply_promo_code(
                stripe_subscription_id=sub.stripe_id,
                promo_code=ser.validated_data["promo_code"],
            )

        async_to_sync(_do)()
        return Response(status=status.HTTP_200_OK)
