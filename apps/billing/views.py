"""Billing API views — checkout, portal, subscriptions."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync
from django.core.cache import cache
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
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
from apps.billing.models import Product as ProductModel
from apps.billing.models import Subscription as SubscriptionModel
from apps.billing.repositories import (
    DjangoStripeCustomerRepository,
    DjangoSubscriptionRepository,
)
from apps.billing.serializers import (
    CheckoutRequestSerializer,
    PlanSerializer,
    PortalRequestSerializer,
    ProductSerializer,
    PromoCodeSerializer,
    SubscriptionSerializer,
    UpdateSubscriptionSerializer,
)
from helpers import get_user

MIN_TEAM_SEATS = 2

_customer_repo = DjangoStripeCustomerRepository()
_subscription_repo = DjangoSubscriptionRepository()


def _validate_quantity_for_plan(plan_price: PlanPrice, quantity: int) -> int:
    """Enforce seat rules: personal plans always 1, team plans >= MIN_TEAM_SEATS."""
    if plan_price.plan.context == PlanContext.PERSONAL:
        if quantity != 1:
            raise ValidationError("Personal plans do not support multiple seats.")
        return 1
    if quantity < MIN_TEAM_SEATS:
        raise ValidationError(f"Team plans require at least {MIN_TEAM_SEATS} seats.")
    return quantity


async def _get_customer_and_subscription(
    user_id: UUID,
) -> tuple[StripeCustomer, Subscription]:
    """Fetch the Stripe customer and active *paid* subscription, or raise NotFound.

    Free-plan (local) subscriptions are excluded because PATCH/DELETE/promo
    operations require a real Stripe subscription.
    """
    customer = await _customer_repo.get_by_user_id(user_id)
    if customer is None:
        raise NotFound("No Stripe customer found.")
    sub = await _subscription_repo.get_active_for_customer(customer.id)
    if sub is None or sub.is_free:
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
    """GET /api/v1/billing/plans — list active plans with prices (public)."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]  # DRF declares as instance var; ClassVar needed for RUF012

    @extend_schema(responses=PlanSerializer(many=True), tags=["billing"])
    def get(self, request: Request) -> Response:
        data = cache.get("active_plans")
        if data is None:
            plans = PlanModel.objects.filter(is_active=True).prefetch_related("prices")
            data = PlanSerializer(plans, many=True).data
            cache.set("active_plans", data, timeout=300)
        return Response(data)


class ProductListView(APIView):
    """GET /api/v1/billing/products — list active one-time products with prices."""

    @extend_schema(responses=ProductSerializer(many=True), tags=["billing"])
    def get(self, request: Request) -> Response:
        data = cache.get("active_products")
        if data is None:
            products = ProductModel.objects.filter(is_active=True).prefetch_related("prices")
            data = ProductSerializer(products, many=True).data
            cache.set("active_products", data, timeout=300)
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
        quantity = _validate_quantity_for_plan(plan_price, data["quantity"])

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
                quantity=quantity,
                promo_code=data["promo_code"],
                locale=user.preferred_locale,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
                trial_period_days=trial_period_days,
            )

        url = async_to_sync(_do)()
        return Response({"url": url}, status=status.HTTP_201_CREATED, headers={"Location": url})


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
        return Response({"url": url}, status=status.HTTP_201_CREATED, headers={"Location": url})


class SubscriptionView(APIView):
    """GET/PATCH/DELETE /api/v1/billing/subscription — manage the current subscription."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(responses={200: SubscriptionSerializer, 404: None}, tags=["billing"])
    def get(self, request: Request) -> Response:
        from django.db.models import Q

        user = get_user(request)
        customer_id = getattr(getattr(user, "stripe_customer", None), "id", None)

        q = Q(user=user)
        if customer_id is not None:
            q |= Q(stripe_customer_id=customer_id)

        try:
            sub = (
                SubscriptionModel.objects.select_related("plan")
                .filter(q, status__in=ACTIVE_SUBSCRIPTION_STATUSES)
                .latest("created_at")
            )
            return Response(SubscriptionSerializer(sub).data)
        except SubscriptionModel.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

    @extend_schema(request=UpdateSubscriptionSerializer, responses={204: None}, tags=["billing"])
    def patch(self, request: Request) -> Response:
        user = get_user(request)
        ser = UpdateSubscriptionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        plan_price = (
            _get_active_plan_price(data["plan_price_id"]) if "plan_price_id" in data else None
        )

        if plan_price and "quantity" in data:
            _validate_quantity_for_plan(plan_price, data["quantity"])

        async def _do() -> None:
            _, sub = await _get_customer_and_subscription(user.id)
            stripe_sub_id: str = sub.stripe_id  # type: ignore[assignment]  # guaranteed non-None by _get_customer_and_subscription
            if plan_price:
                await change_plan(
                    stripe_subscription_id=stripe_sub_id,
                    new_stripe_price_id=data["plan_price_id"],
                    prorate=data["prorate"],
                    quantity=data.get("quantity"),
                )
            elif "quantity" in data:
                await update_seat_count(
                    stripe_subscription_id=stripe_sub_id,
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
            stripe_sub_id: str = sub.stripe_id  # type: ignore[assignment]  # guaranteed non-None by _get_customer_and_subscription
            await apply_promo_code(
                stripe_subscription_id=stripe_sub_id,
                promo_code=ser.validated_data["promo_code"],
            )

        async_to_sync(_do)()
        return Response(status=status.HTTP_200_OK)
