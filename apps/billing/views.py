"""Billing API views — checkout, portal, subscriptions."""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
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
    resume_subscription,
)
from saasmint_core.services.currency import SUPPORTED_CURRENCIES
from saasmint_core.services.subscriptions import (
    change_plan,
    update_seat_count,
)

from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, ExchangeRate, PlanContext, PlanPrice
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
    SubscriptionSerializer,
    UpdateSubscriptionSerializer,
)
from apps.users.models import AccountType
from helpers import get_user

logger = logging.getLogger(__name__)

MIN_TEAM_SEATS = 1

_CURRENCY_PARAM = OpenApiParameter(
    name="currency",
    description="ISO 4217 currency code (e.g. 'eur'). Overrides user preference.",
    required=False,
    type=str,
)


def _resolve_display_currency(request: Request) -> str:
    """Resolve the display currency for a request.

    Priority for anonymous: ``?currency=`` query param → ``"usd"``.
    Priority for authenticated: ``?currency=`` → ``user.preferred_currency`` → ``"usd"``.
    """
    qp_raw = request.query_params.get("currency")
    if qp_raw is not None and qp_raw != "":
        qp = qp_raw.lower()
        if qp not in SUPPORTED_CURRENCIES:
            raise ValidationError({"currency": [f"Unsupported currency: {qp_raw!r}."]})
        return qp

    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        preferred: str | None = getattr(user, "preferred_currency", None)
        if preferred and preferred.lower() in SUPPORTED_CURRENCIES:
            return preferred.lower()

    return "usd"


def _get_exchange_rate(currency: str) -> tuple[str, float]:
    """Return ``(currency, rate)`` for conversion from USD.

    Rates are cached for 10 minutes (they update hourly via Celery beat).
    Falls back to ``("usd", 1.0)`` if the rate is unavailable.
    """
    if currency == "usd":
        return "usd", 1.0

    cache_key = f"exchange_rate:{currency}"
    cached: float | None = cache.get(cache_key)
    if cached is not None:
        return currency, cached

    try:
        er = ExchangeRate.objects.get(currency=currency)
        rate = float(er.rate)
        cache.set(cache_key, rate, timeout=600)
        return currency, rate
    except ExchangeRate.DoesNotExist:
        logger.warning("No exchange rate found for %s, falling back to USD", currency)
        return "usd", 1.0


def _currency_context(request: Request) -> dict[str, object]:
    """Build serializer context dict with currency and rate."""
    currency, rate = _get_exchange_rate(_resolve_display_currency(request))
    return {"currency": currency, "rate": rate}


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


async def _get_customer_and_paid_subscription(
    user_id: UUID,
) -> tuple[StripeCustomer, Subscription, str]:
    """Fetch the Stripe customer, active *paid* subscription, and its stripe_id.

    Free-plan (local) subscriptions are excluded because PATCH/DELETE
    operations require a real Stripe subscription. Returning ``stripe_sub_id``
    as a non-optional ``str`` lets callers avoid re-checking for ``None``.
    Raises NotFound when the customer or paid sub is missing.
    """
    customer = await _customer_repo.get_by_user_id(user_id)
    if customer is None:
        raise NotFound("No Stripe customer found.")
    sub = await _subscription_repo.get_active_for_customer(customer.id)
    if sub is None or sub.stripe_id is None:
        raise NotFound("No active subscription found.")
    return customer, sub, sub.stripe_id


def _get_active_plan_price(plan_price_id: UUID) -> PlanPrice:
    """Validate a PlanPrice with *plan_price_id* exists and belongs to an active plan."""
    plan_price = (
        PlanPrice.objects.select_related("plan")
        .filter(id=plan_price_id, plan__is_active=True)
        .first()
    )
    if plan_price is None:
        raise NotFound("Invalid plan price.")
    return plan_price


class PlanListView(APIView):
    """GET /api/v1/billing/plans — list active plans with prices (public)."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]  # DRF declares as instance var; ClassVar needed for RUF012

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses=inline_serializer(
            "PlanListResponse",
            {"results": PlanSerializer(many=True)},
        ),
        description=(
            "List all active plans with prices. Returned as a non-paginated"
            " ``{results: [...]}`` envelope — the catalog is bounded to a small number of plans."
        ),
        tags=["billing"],
        auth=[],
    )
    def get(self, request: Request) -> Response:
        qs = PlanModel.objects.filter(is_active=True).select_related("price")

        # Authenticated users only see plans matching their account type
        if request.user.is_authenticated:
            context_filter = (
                PlanContext.TEAM
                if request.user.account_type == AccountType.ORG_MEMBER
                else PlanContext.PERSONAL
            )
            qs = qs.filter(context=context_filter)

        data = PlanSerializer(qs, many=True, context=_currency_context(request)).data
        return Response({"results": data})


class ProductListView(APIView):
    """GET /api/v1/billing/products — list active one-time products with prices."""

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses=inline_serializer(
            "ProductListResponse",
            {"results": ProductSerializer(many=True)},
        ),
        description=(
            "List all active one-time products with prices. Returned as a non-paginated"
            " ``{results: [...]}`` envelope — the catalog is bounded to a small number of products."
        ),
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        products = ProductModel.objects.filter(is_active=True).select_related("price")
        data = ProductSerializer(products, many=True, context=_currency_context(request)).data
        return Response({"results": data})


class CheckoutSessionView(APIView):
    """POST /api/v1/billing/checkout-sessions — create a Stripe Checkout Session."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={200: inline_serializer("CheckoutResponse", {"url": drf_serializers.URLField()})},
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = CheckoutRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        plan_price = _get_active_plan_price(data["plan_price_id"])
        quantity = _validate_quantity_for_plan(plan_price, data["quantity"])

        is_team = plan_price.plan.context == PlanContext.TEAM

        # Enforce account_type / plan context match
        if is_team and user.account_type != AccountType.ORG_MEMBER:
            raise ValidationError(
                {
                    "detail": "Only org accounts can check out team plans. "
                    "Register at /api/v1/auth/register/org-owner/ first."
                }
            )
        if not is_team and user.account_type != AccountType.PERSONAL:
            raise ValidationError({"detail": "Org accounts cannot check out personal plans."})

        # Team plans require org_name
        if is_team:
            if "org_name" not in data:
                raise ValidationError({"org_name": ["Required for team plans."]})

        # Orgs are not eligible for trial periods
        trial_period_days = data["trial_period_days"]
        if trial_period_days is not None and is_team:
            trial_period_days = None

        # Build metadata for the checkout session
        metadata: dict[str, str] | None = None
        if is_team:
            metadata = {
                "org_name": data["org_name"],
            }

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
                price_id=plan_price.stripe_price_id,
                quantity=quantity,
                locale=user.preferred_locale,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
                trial_period_days=trial_period_days,
                metadata=metadata,
            )

        url = async_to_sync(_do)()
        return Response({"url": url})


class PortalSessionView(APIView):
    """POST /api/v1/billing/portal-sessions — create a Stripe Customer Portal session."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(
        request=PortalRequestSerializer,
        responses={200: inline_serializer("PortalResponse", {"url": drf_serializers.URLField()})},
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
        return Response({"url": url})


def _get_active_subscription_for_user(user: object) -> SubscriptionModel:
    """Fetch the latest active subscription for a user (paid or free)."""
    customer_id = getattr(getattr(user, "stripe_customer", None), "id", None)
    q = Q(user=user)
    if customer_id is not None:
        q |= Q(stripe_customer_id=customer_id)
    try:
        return (
            SubscriptionModel.objects.select_related("plan__price")
            .filter(q, status__in=ACTIVE_SUBSCRIPTION_STATUSES)
            .latest("created_at")
        )
    except SubscriptionModel.DoesNotExist as exc:
        raise NotFound("No active subscription found.") from exc


class SubscriptionView(APIView):
    """GET/PATCH/DELETE /api/v1/billing/subscriptions/me/ — manage current subscription."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "billing"

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses={200: SubscriptionSerializer},
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        user = get_user(request)
        sub = _get_active_subscription_for_user(user)
        return Response(SubscriptionSerializer(sub, context=_currency_context(request)).data)

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        request=UpdateSubscriptionSerializer,
        responses={200: SubscriptionSerializer},
        tags=["billing"],
    )
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
            customer, sub, stripe_sub_id = await _get_customer_and_paid_subscription(user.id)
            if "cancel_at_period_end" in data:
                if data["cancel_at_period_end"]:
                    await cancel_subscription(
                        stripe_customer_id=customer.id,
                        at_period_end=True,
                        subscription_repo=_subscription_repo,
                    )
                else:
                    await resume_subscription(
                        stripe_customer_id=customer.id,
                        subscription_repo=_subscription_repo,
                    )
            elif plan_price:
                await change_plan(
                    stripe_subscription_id=stripe_sub_id,
                    new_stripe_price_id=plan_price.stripe_price_id,
                    prorate=data["prorate"],
                    quantity=data.get("quantity"),
                )
            elif "quantity" in data:
                # Seat-only update: enforce per-context seat rules against the
                # current subscription's plan, otherwise a personal sub could
                # be bumped to N seats and a team sub down to 1.
                current_price = await PlanPrice.objects.select_related("plan").aget(
                    plan_id=sub.plan_id
                )
                _validate_quantity_for_plan(current_price, data["quantity"])
                await update_seat_count(
                    stripe_subscription_id=stripe_sub_id,
                    quantity=data["quantity"],
                )

        async_to_sync(_do)()
        sub = _get_active_subscription_for_user(user)
        return Response(SubscriptionSerializer(sub, context=_currency_context(request)).data)

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        request=None,
        responses={200: SubscriptionSerializer},
        tags=["billing"],
    )
    def delete(self, request: Request) -> Response:
        user = get_user(request)

        async def _do() -> None:
            customer, _, _ = await _get_customer_and_paid_subscription(user.id)
            await cancel_subscription(
                stripe_customer_id=customer.id,
                at_period_end=True,
                subscription_repo=_subscription_repo,
            )

        async_to_sync(_do)()
        sub = _get_active_subscription_for_user(user)
        return Response(SubscriptionSerializer(sub, context=_currency_context(request)).data)
