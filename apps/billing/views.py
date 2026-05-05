"""Billing API views — checkout, portal, subscriptions."""

from __future__ import annotations

import logging
from typing import Any, ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.db.models import Count, OuterRef, Prefetch, QuerySet, Subquery
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.subscription import Subscription
from saasmint_core.exceptions import SeatsBelowMemberCountError
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    create_product_checkout_session,
    create_team_stripe_customer,
    get_or_create_customer,
    release_pending_schedule_for_customer,
    resume_subscription,
)
from saasmint_core.services.currency import SUPPORTED_CURRENCIES
from saasmint_core.services.subscriptions import (
    change_plan,
    update_seat_count,
)

from apps.base_views import BillingScopedView
from apps.billing.models import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    LocalizedPrice,
    PlanContext,
    PlanPrice,
    ProductPrice,
)
from apps.billing.models import Plan as PlanModel
from apps.billing.models import Product as ProductModel
from apps.billing.models import Subscription as SubscriptionModel
from apps.billing.repositories import get_billing_repos
from apps.billing.serializers import (
    CheckoutRequestSerializer,
    CreditBalanceSerializer,
    PlanSerializer,
    PortalRequestSerializer,
    ProductCheckoutRequestSerializer,
    ProductSerializer,
    SubscriptionSerializer,
    UpdateSubscriptionSerializer,
)
from apps.billing.services import get_credit_balance
from apps.billing.tasks import send_subscription_cancel_notice_task
from apps.users.models import User
from helpers import get_user

logger = logging.getLogger(__name__)

MIN_TEAM_SEATS = 1


class _OrgAlreadyOwned(APIException):
    """409 — caller already owns an org and cannot start a second team checkout (rule 8).

    Raising a bare ``ValidationError({"detail": "..."})`` would coerce the
    string into a list (``{"detail": ["..."]}``) and escape past the custom
    exception middleware, leaking DRF's internal shape. A typed
    ``APIException`` keeps the envelope flat and carries a stable ``code``.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "You already own an organization."
    default_code = "org_already_owned"


_CURRENCY_PARAM = OpenApiParameter(
    name="currency",
    description="ISO 4217 currency code (e.g. 'eur'). Overrides user preference.",
    required=False,
    type=str,
)

_SUBSCRIPTION_CONTEXT_PARAM = OpenApiParameter(
    name="context",
    description=(
        "Which subscription to mutate when the caller has both a personal and "
        "a team subscription concurrently (rule 5a / 5b). One of "
        "``personal`` or ``team``. Defaults to ``team`` for org-member "
        "callers and ``personal`` otherwise. Ignored on GET — that endpoint "
        "always returns every active subscription the caller can see."
    ),
    required=False,
    type=str,
    enum=["personal", "team"],
)


def _resolve_billing_currency(
    query_currency: str | None,
    user: User | None,
) -> tuple[str, str | None]:
    """Resolve the *billing* currency (the one Stripe actually charges).

    Precedence: explicit query param → ``user.preferred_currency`` → ``"usd"``.
    One extra constraint: the resolved currency must be in
    :data:`settings.BILLING_CURRENCIES`. If it isn't, falls back to USD with a
    logged warning.

    Returns ``(billing_currency, preferred_for_dual_display)``. The second
    element is the user's ``preferred_currency`` only when it differs from
    the resolved billing currency (i.e. fallback occurred); the catalog
    serializer uses it to populate the ``local_*`` fields on the response so
    the FE can render a dual-currency card. When billing matches preference,
    the second element is ``None`` and the FE shows a single-line card.
    """
    requested = query_currency.lower() if query_currency else None
    if requested is not None:
        if requested not in SUPPORTED_CURRENCIES:
            raise ValidationError(
                {"currency": [f"Unsupported currency: {query_currency!r}."]}
            )
    user_pref: str | None = None
    if user is not None and user.preferred_currency:
        candidate = user.preferred_currency.lower()
        if candidate in SUPPORTED_CURRENCIES:
            user_pref = candidate

    target = requested or user_pref or "usd"
    if target in settings.BILLING_CURRENCIES:
        billing = target
    else:
        logger.info(
            "Currency %s not in BILLING_CURRENCIES; falling back to usd for charge",
            target,
        )
        billing = "usd"

    # Dual display only when the user's preference differs from what we charge.
    dual_pref = user_pref if user_pref and user_pref != billing else None
    return billing, dual_pref


def _currency_context(request: Request) -> dict[str, Any]:
    """Build serializer context dict with the resolved billing + preferred currencies.

    ``currency``: the *billing* currency the customer is (or would be) charged
    in (``str``). The serializer reads precomputed ``LocalizedPrice`` rows
    for this currency via the prefetched ``localized_prices`` reverse
    relation. No FX math at request time.

    ``preferred_currency``: the user's preferred currency (``str | None``)
    *only* when it differs from the billing currency (fallback case). Drives
    the dual-display ``local_*`` fields on the price serializer.

    Returns ``dict[str, Any]`` to match DRF's serializer-context contract;
    callers should treat ``ctx["currency"]`` as ``str`` and
    ``ctx.get("preferred_currency")`` as ``str | None``.
    """
    user: User | None = request.user if request.user.is_authenticated else None
    billing, dual_pref = _resolve_billing_currency(request.query_params.get("currency"), user)
    return {"currency": billing, "preferred_currency": dual_pref}


def _localized_prices_queryset(
    currency: str, preferred_currency: str | None = None
) -> QuerySet[LocalizedPrice]:
    """Build the LocalizedPrice queryset for prefetching.

    Includes the billing currency row (if non-USD) and the preferred-currency
    row (when set, drives the dual-display ``local_*`` fields). USD never
    needs a row — the serializer short-circuits to the catalog ``amount``.
    Returning a multi-currency queryset is fine: each price has at most one
    row per currency, so the prefetched list is at most length 2.
    """
    wanted = {c for c in (currency, preferred_currency) if c and c != "usd"}
    if not wanted:
        return LocalizedPrice.objects.none()
    return LocalizedPrice.objects.filter(currency__in=wanted)


def _localized_prices_prefetch(
    currency: str, preferred_currency: str | None = None
) -> Prefetch[str]:
    """Prefetch the LocalizedPrice rows for the billing + preferred currencies."""
    return Prefetch(
        "price__localized_prices",
        queryset=_localized_prices_queryset(currency, preferred_currency),
    )


def _localized_subscription_prefetches(
    currency: str, preferred_currency: str | None = None
) -> list[Prefetch[str]]:
    """Prefetches for SubscriptionSerializer's nested plan + scheduled_plan prices."""
    qs = _localized_prices_queryset(currency, preferred_currency)
    return [
        Prefetch("plan__price__localized_prices", queryset=qs),
        Prefetch("scheduled_plan__price__localized_prices", queryset=qs),
    ]


def _resolve_plan_stripe_price_id(plan_price: PlanPrice, currency: str) -> str:
    """Return the Stripe Price ID for *plan_price* in *currency*.

    USD reads ``plan_price.stripe_price_id`` (the historical column). Other
    currencies read ``LocalizedPrice.stripe_price_id`` for the matching pair;
    if absent (currency not billable, or sync_stripe_catalog hasn't minted it
    yet), falls back to the USD ID so the customer can still complete checkout.
    """
    if currency == "usd":
        return plan_price.stripe_price_id
    row = (
        LocalizedPrice.objects.filter(
            plan_price=plan_price, currency=currency, stripe_price_id__isnull=False
        )
        .only("stripe_price_id")
        .first()
    )
    return row.stripe_price_id if row and row.stripe_price_id else plan_price.stripe_price_id


def _resolve_product_stripe_price_id(product_price: ProductPrice, currency: str) -> str:
    """Return the Stripe Price ID for *product_price* in *currency*. See plan variant."""
    if currency == "usd":
        return product_price.stripe_price_id
    row = (
        LocalizedPrice.objects.filter(
            product_price=product_price, currency=currency, stripe_price_id__isnull=False
        )
        .only("stripe_price_id")
        .first()
    )
    return row.stripe_price_id if row and row.stripe_price_id else product_price.stripe_price_id


def _resolve_plan_change_price(plan_price: PlanPrice, currency: str) -> tuple[str, int]:
    """Resolve ``(stripe_price_id, unit_amount)`` for a plan change to *currency*.

    Stripe pins a subscription's currency for life, so a PATCH that changes
    the plan must produce a Stripe Price ID in the *same* currency as the
    existing subscription. If the requested plan has no minted Stripe Price
    in that currency, returning a USD fallback would silently flip the
    subscription's currency at modify time — Stripe would reject. So instead
    we raise 400 and let the FE surface the unavailability.

    The returned ``unit_amount`` is in the same currency as the Stripe Price
    so that ``change_plan``'s deferred-downgrade comparison (new vs current
    unit_amount) is apples-to-apples.
    """
    if currency == "usd":
        return plan_price.stripe_price_id, plan_price.amount

    row = (
        LocalizedPrice.objects.filter(
            plan_price=plan_price, currency=currency, stripe_price_id__isnull=False
        )
        .only("stripe_price_id", "amount_minor")
        .first()
    )
    if row is None or not row.stripe_price_id:
        raise ValidationError(
            {"plan_price_id": ["Plan unavailable in your subscription's currency."]}
        )
    return row.stripe_price_id, row.amount_minor


def _validate_quantity_for_context(context: PlanContext, quantity: int) -> int:
    """Enforce seat rules: personal plans always 1, team plans >= MIN_TEAM_SEATS."""
    if context == PlanContext.PERSONAL:
        if quantity != 1:
            raise ValidationError("Personal plans do not support multiple seats.")
        return 1
    if quantity < MIN_TEAM_SEATS:
        raise ValidationError(f"Team plans require at least {MIN_TEAM_SEATS} seats.")
    return quantity


def _validate_quantity_for_plan(plan_price: PlanPrice, quantity: int) -> int:
    return _validate_quantity_for_context(PlanContext(plan_price.plan.context), quantity)


async def _reject_seat_limit_below_member_count(org_id: UUID, seat_limit: int) -> None:
    """Reject a seat reduction that would leave the org over its sub's seat cap.

    Members must be removed before the seat count can be reduced below the
    current head-count — otherwise we'd commit to a state where the sub bills
    for fewer seats than are actually filled.
    """
    from apps.orgs.models import OrgMember

    member_count = await OrgMember.objects.filter(org_id=org_id).acount()
    if seat_limit < member_count:
        raise SeatsBelowMemberCountError(
            f"Cannot reduce seats to {seat_limit}: org has {member_count} members."
            f" Remove {member_count - seat_limit} member(s) first."
        )


_SUBSCRIPTION_CONTEXT_TEAM = "team"
_SUBSCRIPTION_CONTEXT_PERSONAL = "personal"


def _validate_subscription_context(value: str | None) -> str | None:
    """Coerce the ``?context=`` query param to ``personal``/``team``/``None``."""
    if value is None or value == "":
        return None
    if value not in (_SUBSCRIPTION_CONTEXT_TEAM, _SUBSCRIPTION_CONTEXT_PERSONAL):
        raise ValidationError(
            {"context": ["Must be 'personal' or 'team'."]},
        )
    return value


def _user_is_org_member(user: User) -> bool:
    """Return True if *user* belongs to any org.

    The source of truth for "is this caller an org member" — replaces the
    old ``user.account_type == ORG_MEMBER`` denormalized flag. A user is
    an org member iff an ``OrgMember`` row exists for them.
    """
    from apps.orgs.models import OrgMember

    return OrgMember.objects.filter(user_id=user.id).exists()


async def _user_is_org_member_async(user: User) -> bool:
    """Async variant of :func:`_user_is_org_member`."""
    from apps.orgs.models import OrgMember

    return await OrgMember.objects.filter(user_id=user.id).aexists()


def _default_subscription_context(user: User) -> str:
    """Resolve the default ``?context=`` for PATCH/DELETE on /me/.

    Org member → team (existing behavior), non-member → personal. The default
    keeps single-sub callers working unchanged. Concurrent users (rule 5b)
    pass an explicit ``?context=personal`` to manage their personal sub.
    """
    return (
        _SUBSCRIPTION_CONTEXT_TEAM if _user_is_org_member(user) else _SUBSCRIPTION_CONTEXT_PERSONAL
    )


async def _default_subscription_context_async(user: User) -> str:
    """Async variant of :func:`_default_subscription_context`."""
    return (
        _SUBSCRIPTION_CONTEXT_TEAM
        if await _user_is_org_member_async(user)
        else _SUBSCRIPTION_CONTEXT_PERSONAL
    )


async def _resolve_billing_customer(
    user: User, *, context: str | None = None
) -> StripeCustomer | None:
    """Return the StripeCustomer that owns billing for *user*, or None.

    Without ``context``: non-org-member users get their user-scoped customer,
    org members get the customer attached to the active org they belong to.
    With ``context="personal"`` or ``"team"``, returns the matching customer
    regardless of org membership — required for the concurrent-billing case
    (rule 5a/5b) where an org member still has an active personal sub on
    their user-scoped customer.
    """
    repos = get_billing_repos()
    effective = context or await _default_subscription_context_async(user)
    if effective == _SUBSCRIPTION_CONTEXT_TEAM:
        from apps.orgs.models import OrgMember

        membership = (
            await OrgMember.objects.filter(
                user_id=user.id,
            )
            .only("org_id")
            .afirst()
        )
        if membership is None:
            return None
        return await repos.customers.get_by_org_id(membership.org_id)
    return await repos.customers.get_by_user_id(user.id)


async def _get_customer_and_paid_subscription(
    user: User, *, context: str | None = None
) -> tuple[StripeCustomer, Subscription, str]:
    """Fetch the Stripe customer, active subscription, and its stripe_id.

    Without ``context``: same routing as :func:`_resolve_billing_customer`
    defaults. With explicit ``context``, lets concurrent-billing callers
    target either the team or the personal sub. Returning ``stripe_sub_id``
    as a non-optional ``str`` lets callers avoid re-checking for ``None`` —
    every persisted Subscription is a Stripe mirror with a non-null
    stripe_id. Raises NotFound when the customer or subscription is missing.
    """
    repos = get_billing_repos()
    customer = await _resolve_billing_customer(user, context=context)
    if customer is None:
        raise NotFound("No Stripe customer found.")
    sub = await repos.subscriptions.get_active_for_customer(customer.id)
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


def _get_active_product_price(product_price_id: UUID) -> ProductPrice:
    """Validate a ProductPrice with *product_price_id* exists and is active.

    The view only reads ``product_id`` (the FK column, already on the row) and
    ``stripe_price_id`` off the result, so ``select_related("product")`` would
    hydrate a Product we never touch — ``product__is_active=True`` still uses
    a JOIN in the WHERE clause, just without pulling the row into Python.
    """
    product_price = ProductPrice.objects.filter(
        id=product_price_id, product__is_active=True
    ).first()
    if product_price is None:
        raise NotFound("Invalid product price.")
    return product_price


def _catalog_envelope(results: list[dict[str, object]]) -> dict[str, object]:
    """Wrap catalog results in a DRF-style paginated envelope.

    The catalog is bounded, so ``next`` and ``previous`` are always ``None``
    and ``count`` is simply ``len(results)`` — but emitting the same shape as
    real paginated endpoints lets clients share one decoder.
    """
    return {"count": len(results), "next": None, "previous": None, "results": results}


class PlanListView(APIView):
    """GET /api/v1/billing/plans — list active plans with prices (public)."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]  # DRF declares as instance var; ClassVar needed for RUF012

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses=inline_serializer(
            "PlanListResponse",
            {
                "count": drf_serializers.IntegerField(),
                "next": drf_serializers.URLField(allow_null=True),
                "previous": drf_serializers.URLField(allow_null=True),
                "results": PlanSerializer(many=True),
            },
        ),
        description=(
            "List all active plans with prices. Emits the DRF paginated envelope"
            " (``count``/``next``/``previous``/``results``) — the catalog is bounded,"
            " so ``next`` and ``previous`` are always ``null``."
        ),
        tags=["billing"],
        auth=[],
    )
    def get(self, request: Request) -> Response:
        # Personal and team plans are both shown to every caller (auth or anon).
        # Users without an owned org can upgrade to a team plan via team-context
        # checkout (see CheckoutSessionView), so hiding team plans from them
        # would make the upgrade undiscoverable.
        ctx = _currency_context(request)
        qs = (
            PlanModel.objects.filter(is_active=True)
            .select_related("price")
            .prefetch_related(
                _localized_prices_prefetch(ctx["currency"], ctx.get("preferred_currency"))
            )
        )
        data = PlanSerializer(qs, many=True, context=ctx).data
        return Response(_catalog_envelope(list(data)))


class ProductListView(APIView):
    """GET /api/v1/billing/products — list active one-time products with prices."""

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses=inline_serializer(
            "ProductListResponse",
            {
                "count": drf_serializers.IntegerField(),
                "next": drf_serializers.URLField(allow_null=True),
                "previous": drf_serializers.URLField(allow_null=True),
                "results": ProductSerializer(many=True),
            },
        ),
        description=(
            "List all active one-time products with prices. Emits the DRF paginated envelope"
            " (``count``/``next``/``previous``/``results``) — the catalog is bounded,"
            " so ``next`` and ``previous`` are always ``null``."
        ),
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        ctx = _currency_context(request)
        products = (
            ProductModel.objects.filter(is_active=True)
            .select_related("price")
            .prefetch_related(
                _localized_prices_prefetch(ctx["currency"], ctx.get("preferred_currency"))
            )
        )
        data = ProductSerializer(products, many=True, context=ctx).data
        return Response(_catalog_envelope(list(data)))


class BillingCurrenciesView(APIView):
    """GET /api/v1/billing/currencies — list billable + display-only currencies."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]  # DRF declares as instance var; ClassVar needed for RUF012

    @extend_schema(
        responses=inline_serializer(
            "BillingCurrenciesResponse",
            {
                "billable": drf_serializers.ListField(child=drf_serializers.CharField()),
                "display_only": drf_serializers.ListField(child=drf_serializers.CharField()),
            },
        ),
        description=(
            "Currencies the catalog actually charges in (``billable``) versus those"
            " that are catalog-displayable but fall back to USD at Checkout"
            " (``display_only``). Drives the FE currency dropdown."
        ),
        tags=["billing"],
        auth=[],
    )
    def get(self, request: Request) -> Response:
        billable = sorted(settings.BILLING_CURRENCIES)
        display_only = sorted(SUPPORTED_CURRENCIES - set(billable))
        return Response({"billable": billable, "display_only": display_only})


class CheckoutSessionView(BillingScopedView):
    """POST /api/v1/billing/checkout-sessions — create a Stripe Checkout Session."""

    @extend_schema(
        request=CheckoutRequestSerializer,
        parameters=[_CURRENCY_PARAM],
        responses={
            200: inline_serializer("CheckoutResponse", {"url": drf_serializers.URLField()}),
            400: OpenApiResponse(
                description=(
                    "Request body failed validation (e.g. ``org_name`` missing for a"
                    " team-context plan, invalid quantity for the plan's context),"
                    " or unsupported ``?currency=`` value."
                )
            ),
            404: OpenApiResponse(description="Invalid plan price."),
            409: OpenApiResponse(
                description=(
                    "Caller already owns an organization and cannot start a"
                    " second team checkout (rule 8) — ``code=org_already_owned``."
                )
            ),
        },
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = CheckoutRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        plan_price = _get_active_plan_price(data["plan_price_id"])
        quantity = _validate_quantity_for_plan(plan_price, data["seat_limit"])

        billing_currency, _ = _resolve_billing_currency(
            request.query_params.get("currency"), user
        )
        stripe_price_id = _resolve_plan_stripe_price_id(plan_price, billing_currency)

        is_team = plan_price.plan.context == PlanContext.TEAM

        # Team plans: any user without an owned org may upgrade (rule 8: one
        # owned org per user). The DB partial unique index on OrgMember.user
        # WHERE role='owner' is the authoritative enforcer; this check is a
        # fast-path UX guard. Personal-plan checkouts have no eligibility
        # gate — rule 5b allows org members to also hold a personal sub.
        if is_team:
            from apps.orgs.models import OrgMember, OrgRole

            already_owns_org = OrgMember.objects.filter(
                user_id=user.id, role=OrgRole.OWNER
            ).exists()
            if already_owns_org:
                raise _OrgAlreadyOwned

            if "org_name" not in data:
                raise ValidationError({"org_name": ["Required for team plans."]})

        # Orgs are not eligible for trial periods
        trial_period_days = data["trial_period_days"]
        if trial_period_days is not None and is_team:
            trial_period_days = None

        # Build metadata for the checkout session. Stripe metadata values are
        # strings — booleans go through as "true"/"false" and are parsed back
        # on the webhook side.
        metadata: dict[str, str] | None = None
        if is_team:
            metadata = {
                "org_name": data["org_name"],
                "keep_personal_subscription": "true"
                if data["keep_personal_subscription"]
                else "false",
            }

        async def _do() -> str:
            if is_team:
                stripe_customer_id = await create_team_stripe_customer(
                    user_id=user.id,
                    email=str(user.email),
                    name=user.full_name,
                    locale=user.preferred_locale,
                )
            else:
                customer = await get_or_create_customer(
                    user_id=user.id,
                    email=str(user.email),
                    name=user.full_name,
                    locale=user.preferred_locale,
                    customer_repo=get_billing_repos().customers,
                )
                stripe_customer_id = customer.stripe_id
            return await create_checkout_session(
                stripe_customer_id=stripe_customer_id,
                client_reference_id=str(user.id),
                price_id=stripe_price_id,
                billing_currency=billing_currency,
                quantity=quantity,
                locale=user.preferred_locale,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
                trial_period_days=trial_period_days,
                metadata=metadata,
            )

        url = async_to_sync(_do)()
        return Response({"url": url})


class PortalSessionView(BillingScopedView):
    """POST /api/v1/billing/portal-sessions — create a Stripe Customer Portal session."""

    @extend_schema(
        parameters=[_SUBSCRIPTION_CONTEXT_PARAM],
        request=PortalRequestSerializer,
        responses={
            200: inline_serializer("PortalResponse", {"url": drf_serializers.URLField()}),
            400: OpenApiResponse(
                description=(
                    "The ``?context=`` query param is set to a value other than"
                    " ``personal``/``team``."
                )
            ),
            403: OpenApiResponse(
                description=(
                    "``?context=team``: caller is missing ``is_billing=True`` on their"
                    " active org membership — only billing members may open the team"
                    " portal."
                )
            ),
            404: OpenApiResponse(
                description=(
                    "``?context=team``: caller has no team Stripe customer (i.e. no"
                    " team subscription has been created)."
                )
            ),
        },
        description=(
            "Create a Stripe Customer Portal session. The portal scope is selected"
            " by ``?context=personal|team``; defaults to ``team`` for org-member"
            " callers and ``personal`` otherwise — same routing as subscription"
            " mutations on ``/me/``. ``?context=team`` requires ``is_billing=True``"
            " and an existing team customer. ``?context=personal`` auto-creates"
            " the user's Stripe customer when missing.\n\n"
            "Plan switches are **not** handled here: the portal applies them"
            " immediately with proration, which conflicts with our deferred"
            " downgrade rule. Use ``PATCH /subscriptions/me/`` instead."
        ),
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = PortalRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        context = _validate_subscription_context(request.query_params.get("context"))

        async def _do() -> str:
            effective = context or await _default_subscription_context_async(user)

            if effective == _SUBSCRIPTION_CONTEXT_TEAM:
                # Same gate as cancel/resume: only is_billing members may open
                # the team portal (it exposes payment methods, invoices, and
                # cancel-from-Stripe — not read-only).
                await sync_to_async(_require_billing_authority)(
                    user, context=_SUBSCRIPTION_CONTEXT_TEAM
                )
                customer = await _resolve_billing_customer(
                    user, context=_SUBSCRIPTION_CONTEXT_TEAM
                )
                if customer is None:
                    raise NotFound("No team Stripe customer found.")
                stripe_customer_id = customer.stripe_id
            else:
                # Personal scope: auto-create the user's own customer if missing.
                # Mixing scopes (creating a personal customer for a team-portal
                # request) would silently leak a stub row into the wrong scope.
                personal = await get_or_create_customer(
                    user_id=user.id,
                    email=str(user.email),
                    name=user.full_name,
                    locale=user.preferred_locale,
                    customer_repo=get_billing_repos().customers,
                )
                stripe_customer_id = personal.stripe_id

            return await create_billing_portal_session(
                stripe_customer_id=stripe_customer_id,
                locale=user.preferred_locale,
                return_url=ser.validated_data["return_url"],
            )

        url = async_to_sync(_do)()
        return Response({"url": url})


def _resolve_product_purchase_context(user: User, context: str | None) -> UUID | None:
    """Authorize a credit purchase under the requested context.

    Returns ``org_id`` when the purchase is scoped to an org, ``None`` for a
    personal purchase. Mirrors the ``?context=personal|team`` semantics used
    by subscription mutations (rule 5a/5b).

    Defaults when ``context`` is None: org member → team, non-member → personal.

    ``context=personal`` is universally allowed: anyone can buy credits for
    themselves, including org admins/regular members who cannot buy for the
    org. The owner-only gate only applies to ``context=team``, since only
    owners may spend org funds. Non-org-member callers cannot pick ``team``
    (no org to buy for) and get 400.
    """
    from apps.orgs.models import OrgMember, OrgRole

    effective = context or _default_subscription_context(user)

    if effective == _SUBSCRIPTION_CONTEXT_PERSONAL:
        return None

    # effective == "team" — fetch any membership (regardless of role) in a
    # single query, then discriminate the three outcomes (owner / non-owner /
    # not-a-member) in Python. Selecting ``role`` lets us collapse the prior
    # two-query error path (owner-only filter → exists() fallback) into one.
    membership = OrgMember.objects.filter(user_id=user.id).only("org_id", "role").first()
    if membership is None:
        # Not an org member at all (400 — bad request, no team scope).
        raise ValidationError(
            {"context": ["Only org members can purchase team credits."]},
        )
    if membership.role != OrgRole.OWNER:
        # Org member but not owner — only owners may spend org funds.
        raise PermissionDenied("Only the org owner can purchase credits for the team.")
    return membership.org_id


class ProductCheckoutSessionView(BillingScopedView):
    """POST /api/v1/billing/product-checkout-sessions/ — one-time product purchase."""

    @extend_schema(
        request=ProductCheckoutRequestSerializer,
        parameters=[
            _CURRENCY_PARAM,
            OpenApiParameter(
                name="context",
                description=(
                    "Pick the buyer scope when the caller can purchase under both"
                    " (rule 5a/5b). ``personal`` credits the user's own balance;"
                    " ``team`` credits the org. Default: ``team`` for org members,"
                    " ``personal`` otherwise. ``team`` requires ``role=OWNER`` on"
                    " the active org. Non-org-member callers cannot pick ``team``."
                ),
                required=False,
                type=str,
                enum=["personal", "team"],
            ),
        ],
        responses={
            200: inline_serializer("ProductCheckoutResponse", {"url": drf_serializers.URLField()}),
            400: OpenApiResponse(
                description=(
                    "Invalid ``?context=`` value, non-org-member caller"
                    " requested ``?context=team``, or unsupported ``?currency=`` value."
                )
            ),
            403: OpenApiResponse(
                description=(
                    "``?context=team``: caller is not the org owner. Admins and"
                    " regular members can still buy ``?context=personal``."
                )
            ),
            404: OpenApiResponse(description="Invalid product price."),
        },
        description=(
            "Create a Stripe Checkout Session (``mode=payment``) for a one-time"
            " product purchase (credit pack). The buyer scope is selected by"
            " ``?context=personal|team``; defaults to ``team`` for org members"
            " and ``personal`` otherwise. ``personal`` is universally allowed"
            " (anyone can buy credits for themselves); ``team`` is restricted"
            " to org owners."
        ),
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = ProductCheckoutRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        context = _validate_subscription_context(request.query_params.get("context"))
        product_price = _get_active_product_price(data["product_price_id"])
        org_id = _resolve_product_purchase_context(user, context)

        billing_currency, _ = _resolve_billing_currency(
            request.query_params.get("currency"), user
        )
        stripe_price_id = _resolve_product_stripe_price_id(product_price, billing_currency)

        metadata: dict[str, str] = {"product_id": str(product_price.product_id)}
        if org_id is not None:
            metadata["org_id"] = str(org_id)

        async def _do() -> str:
            customer_kwargs: dict[str, object] = {
                "email": str(user.email),
                "name": user.full_name,
                "locale": user.preferred_locale,
                "customer_repo": get_billing_repos().customers,
            }
            if org_id is not None:
                customer_kwargs["org_id"] = org_id
            else:
                customer_kwargs["user_id"] = user.id
            customer = await get_or_create_customer(**customer_kwargs)  # type: ignore[arg-type]  # dynamic kwargs: user_id or org_id set by branch above
            return await create_product_checkout_session(
                stripe_customer_id=customer.stripe_id,
                client_reference_id=str(user.id),
                price_id=stripe_price_id,
                billing_currency=billing_currency,
                locale=user.preferred_locale,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
                metadata=metadata,
            )

        url = async_to_sync(_do)()
        return Response({"url": url})


class CreditBalanceView(BillingScopedView):
    """GET /api/v1/billing/credits/me/ — read the caller's credit balance."""

    @extend_schema(
        responses={200: CreditBalanceSerializer},
        description=(
            "Return the caller's current credit balances as a list. Non-org-member"
            " users see a single entry with their own balance. Org members see"
            " their org's balance (readable by any active member), plus a"
            " ``user``-scoped entry iff a personal balance survives from before"
            " a personal→team upgrade (rule 16) — this entry is omitted when"
            " the user has no leftover personal credits."
        ),
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        from apps.orgs.models import OrgMember

        user = get_user(request)
        balances: list[dict[str, object]] = []

        # Fetch only the org_id — get_credit_balance filters by FK, so we
        # don't need to hydrate the full Org row via select_related.
        org_id = OrgMember.objects.filter(user_id=user.id).values_list("org_id", flat=True).first()
        if org_id is not None:
            balances.append({"balance": get_credit_balance(org_id=org_id), "scope": "org"})
            # Surface leftover personal credits from a pre-upgrade purchase
            # (rule 16). Only emit when > 0 so we don't spam zero-rows for
            # org members who never had a personal balance.
            personal_balance = get_credit_balance(user=user)
            if personal_balance > 0:
                balances.append({"balance": personal_balance, "scope": "user"})
        else:
            balances.append({"balance": get_credit_balance(user=user), "scope": "user"})

        return Response(CreditBalanceSerializer({"balances": balances}).data)


def _get_active_subscriptions_for_user(
    user: User, *, currency: str = "usd", preferred_currency: str | None = None
) -> list[SubscriptionModel]:
    """Return every active subscription the user has billing visibility into.

    A user can hold up to two concurrent active subscriptions (rules 5a/5b
    + 16):
      - The **team** sub on their org's Stripe customer (any member of the
        active org sees it; the is_billing gate applies to mutations only).
      - The **personal** sub on their own user-scoped Stripe customer (any
        user may have this; org members may also retain one when they keep
        personal running concurrently).

    Returns 0, 1, or 2 subs (team first when present, then personal). An
    empty list is the new free-tier shape (replaces the old NotFound 404 on
    ``GET /me/``).
    """
    from apps.orgs.models import OrgMember

    # Annotate the org member count onto each sub so ``SubscriptionSerializer
    # .get_seats_used`` can read it as a plain attribute instead of firing a
    # separate COUNT query per serialized object.
    org_member_count_sq = (
        OrgMember.objects.filter(org_id=OuterRef("stripe_customer__org_id"))
        .order_by()
        .values("org_id")
        .annotate(n=Count("id"))
        .values("n")
    )

    # ``stripe_customer`` is select_related so ``_refetch_subscription_after_mutation``
    # can discriminate team vs personal subs via ``sub.stripe_customer.org_id``
    # without firing an FK lookup per sub.
    base = SubscriptionModel.objects.select_related(
        "plan__price", "scheduled_plan__price", "stripe_customer"
    ).prefetch_related(
        *_localized_subscription_prefetches(currency, preferred_currency)
    ).annotate(
        org_member_count=Subquery(org_member_count_sq)
    ).filter(
        status__in=ACTIVE_SUBSCRIPTION_STATUSES
    )
    subs: list[SubscriptionModel] = []
    seen_ids: set[UUID] = set()

    membership = OrgMember.objects.filter(user_id=user.id).only("org_id").first()
    if membership is not None:
        team_sub = (
            base.filter(stripe_customer__org_id=membership.org_id).order_by("-created_at").first()
        )
        if team_sub is not None:
            subs.append(team_sub)
            seen_ids.add(team_sub.id)

    # Personal sub — picked up via either ``Subscription.user_id`` or
    # ``stripe_customer.user_id``. Split into two queries so each can use its
    # own partial index (idx_sub_user_status / idx_sub_customer_status)
    # instead of degenerating into a scan on an OR'd predicate.
    customer = getattr(user, "stripe_customer", None)
    customer_id = customer.id if customer is not None else None
    sub_user = base.filter(user_id=user.id).order_by("-created_at").first()
    sub_customer = (
        base.filter(stripe_customer_id=customer_id).order_by("-created_at").first()
        if customer_id is not None
        else None
    )
    personal_candidates = [s for s in (sub_user, sub_customer) if s is not None]
    if personal_candidates:
        latest_personal = max(personal_candidates, key=lambda s: s.created_at)
        if latest_personal.id not in seen_ids:
            subs.append(latest_personal)

    return subs


def _require_billing_authority(user: User, *, context: str) -> UUID | None:
    """Enforce that *user* may mutate the subscription in *context*.

    For ``context="team"``: requires ``is_billing=True`` on an active
    org membership. Returns the ``org_id`` (used to address the
    notification recipient list).

    For ``context="personal"``: anyone may mutate their own personal sub.
    Returns ``None``.
    """
    if context == _SUBSCRIPTION_CONTEXT_PERSONAL:
        return None

    from apps.orgs.models import OrgMember

    billing_member = (
        OrgMember.objects.filter(
            user_id=user.id,
            is_billing=True,
        )
        .only("org_id")
        .first()
    )
    if billing_member is None:
        raise PermissionDenied("Only billing members can modify the team subscription.")
    return billing_member.org_id


def _resolve_mutation_context(request: Request, user: User) -> tuple[str, UUID | None]:
    """Resolve ``?context=`` and enforce billing authority for the chosen context.

    Shared prologue for ``PATCH`` and ``DELETE`` on ``/me/`` — both endpoints
    parse the same query param and run the same authority check, so keeping
    them in lockstep here avoids drift if the gate ever needs to grow (e.g.
    new context value, new role check).

    When ``?context=`` is omitted, the default-context lookup and the
    is_billing authority check both target the same OrgMember row, so we
    fetch it once and derive both — saves one round-trip on every PATCH /
    DELETE without an explicit context.
    """
    explicit = _validate_subscription_context(request.query_params.get("context"))
    if explicit is not None:
        return explicit, _require_billing_authority(user, context=explicit)

    from apps.orgs.models import OrgMember

    membership = OrgMember.objects.filter(user_id=user.id).only("org_id", "is_billing").first()
    if membership is None:
        # No org → default is personal, no authority gate.
        return _SUBSCRIPTION_CONTEXT_PERSONAL, None
    # Org member → default is team; reuse the same row for the is_billing gate.
    if not membership.is_billing:
        raise PermissionDenied("Only billing members can modify the team subscription.")
    return _SUBSCRIPTION_CONTEXT_TEAM, membership.org_id


def _billing_notice_recipients(user: User, org_id: UUID | None) -> list[str]:
    """Return the list of emails to notify on a billing-state change.

    Personal-context subs: just the owner. Team-context subs: every
    ``is_billing=True`` member of the org (so a rogue billing contact's
    action is visible to peers).
    """
    if org_id is None:
        return [str(user.email)]

    from apps.orgs.models import OrgMember

    return list(
        OrgMember.objects.filter(
            org_id=org_id,
            is_billing=True,
        ).values_list("user__email", flat=True)
    )


class SubscriptionView(BillingScopedView):
    """GET/PATCH/DELETE /api/v1/billing/subscriptions/me/ — manage current subscriptions.

    GET returns every active subscription the caller has billing visibility
    into (0, 1, or 2 — see :func:`_get_active_subscriptions_for_user`).
    PATCH/DELETE accept a ``?context=personal|team`` query param to pick
    which subscription to mutate when the caller has both concurrently.
    """

    @extend_schema(
        parameters=[_CURRENCY_PARAM],
        responses={
            200: inline_serializer(
                "SubscriptionListResponse",
                {
                    "count": drf_serializers.IntegerField(),
                    "next": drf_serializers.URLField(allow_null=True),
                    "previous": drf_serializers.URLField(allow_null=True),
                    "results": SubscriptionSerializer(many=True),
                },
            ),
        },
        description=(
            "List every active subscription the caller has billing visibility"
            " into. Empty ``results`` indicates the free tier. Up to two rows"
            " can appear when a user holds both a personal and a team"
            " subscription concurrently (rule 5a — paid personal user accepts"
            " a team invite — or rule 5b — keep-personal opt-out during a"
            " personal→team upgrade)."
        ),
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        user = get_user(request)
        ctx = _currency_context(request)
        subs = _get_active_subscriptions_for_user(
            user,
            currency=ctx["currency"],
            preferred_currency=ctx.get("preferred_currency"),
        )
        data = SubscriptionSerializer(subs, many=True, context=ctx).data
        return Response(_catalog_envelope(list(data)))

    @extend_schema(
        parameters=[_CURRENCY_PARAM, _SUBSCRIPTION_CONTEXT_PARAM],
        request=UpdateSubscriptionSerializer,
        responses={
            200: SubscriptionSerializer,
            400: OpenApiResponse(
                description=(
                    "Request body failed validation, or the ``?context=`` query param"
                    " is set to a value other than ``personal``/``team``."
                )
            ),
            403: OpenApiResponse(
                description=(
                    "``?context=team``: caller is missing ``is_billing=True`` on their"
                    " active org membership — only billing members may modify the team"
                    " subscription. ``?context=personal`` does not enforce this gate."
                )
            ),
            404: OpenApiResponse(
                description="No Stripe customer or active paid subscription for the caller."
            ),
        },
        tags=["billing"],
    )
    def patch(self, request: Request) -> Response:
        user = get_user(request)
        ser = UpdateSubscriptionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        context, org_id = _resolve_mutation_context(request, user)

        plan_price = (
            _get_active_plan_price(data["plan_price_id"]) if "plan_price_id" in data else None
        )

        if plan_price and "seat_limit" in data:
            _validate_quantity_for_plan(plan_price, data["seat_limit"])

        async def _do() -> None:
            repos = get_billing_repos()
            customer, sub, stripe_sub_id = await _get_customer_and_paid_subscription(
                user, context=context
            )
            if "cancel_at_period_end" in data:
                if data["cancel_at_period_end"]:
                    await cancel_subscription(
                        stripe_customer_id=customer.id,
                        at_period_end=True,
                        subscription_repo=repos.subscriptions,
                    )
                else:
                    await resume_subscription(
                        stripe_customer_id=customer.id,
                        subscription_repo=repos.subscriptions,
                    )
            elif plan_price:
                # Stripe pins the subscription's currency for life — plan
                # changes must resolve a Stripe Price in the same currency.
                # We also feed ``new_price_amount`` in that currency so the
                # deferred-downgrade comparison stays apples-to-apples.
                sub_currency = sub.currency
                new_price_id, new_price_amount = await sync_to_async(
                    _resolve_plan_change_price, thread_sensitive=True
                )(plan_price, sub_currency)
                if "seat_limit" in data and org_id is not None:
                    await _reject_seat_limit_below_member_count(org_id, data["seat_limit"])
                await change_plan(
                    stripe_subscription_id=stripe_sub_id,
                    new_stripe_price_id=new_price_id,
                    new_price_amount=new_price_amount,
                    prorate=data["prorate"],
                    quantity=data.get("seat_limit"),
                )
            elif "seat_limit" in data:
                # Seat-only update: enforce per-context seat rules against the
                # current subscription's plan, otherwise a personal sub could
                # be bumped to N seats and a team sub down to 1.
                current_plan = await PlanModel.objects.only("context").aget(id=sub.plan_id)
                _validate_quantity_for_context(
                    PlanContext(current_plan.context), data["seat_limit"]
                )
                if org_id is not None:
                    await _reject_seat_limit_below_member_count(org_id, data["seat_limit"])
                await update_seat_count(
                    active=sub,
                    quantity=data["seat_limit"],
                    subscription_repo=repos.subscriptions,
                )

        async_to_sync(_do)()
        ctx = _currency_context(request)
        sub = _refetch_subscription_after_mutation(
            user,
            context=context,
            currency=ctx["currency"],
            preferred_currency=ctx.get("preferred_currency"),
        )
        if "cancel_at_period_end" in data:
            recipients = _billing_notice_recipients(user, org_id)
            if recipients:
                send_subscription_cancel_notice_task.delay(
                    recipients,
                    sub.plan.name,
                    "scheduled" if data["cancel_at_period_end"] else "resumed",
                )
        return Response(SubscriptionSerializer(sub, context=ctx).data)

    @extend_schema(
        parameters=[_CURRENCY_PARAM, _SUBSCRIPTION_CONTEXT_PARAM],
        request=None,
        responses={
            202: SubscriptionSerializer,
            400: OpenApiResponse(
                description=(
                    "The ``?context=`` query param is set to a value other than"
                    " ``personal``/``team``."
                )
            ),
            403: OpenApiResponse(
                description=(
                    "``?context=team``: caller is missing ``is_billing=True`` on their"
                    " active org membership — only billing members may cancel the team"
                    " subscription. ``?context=personal`` does not enforce this gate."
                )
            ),
            404: OpenApiResponse(
                description="No Stripe customer or active paid subscription for the caller."
            ),
        },
        description=(
            "Schedule subscription cancellation at the end of the current billing period."
            " Returns 202 Accepted — the subscription remains active until the period end"
            " timestamp returned in the body. Use ``?context=personal`` to cancel the"
            " personal sub when the caller also holds a concurrent team sub."
        ),
        tags=["billing"],
    )
    def delete(self, request: Request) -> Response:
        user = get_user(request)
        context, org_id = _resolve_mutation_context(request, user)

        async def _do() -> None:
            customer, _, _ = await _get_customer_and_paid_subscription(user, context=context)
            await cancel_subscription(
                stripe_customer_id=customer.id,
                at_period_end=True,
                subscription_repo=get_billing_repos().subscriptions,
            )

        async_to_sync(_do)()
        ctx = _currency_context(request)
        sub = _refetch_subscription_after_mutation(
            user,
            context=context,
            currency=ctx["currency"],
            preferred_currency=ctx.get("preferred_currency"),
        )
        recipients = _billing_notice_recipients(user, org_id)
        if recipients:
            send_subscription_cancel_notice_task.delay(recipients, sub.plan.name, "scheduled")
        return Response(
            SubscriptionSerializer(sub, context=ctx).data,
            status=status.HTTP_202_ACCEPTED,
        )


class ScheduledChangeView(BillingScopedView):
    """DELETE /api/v1/billing/subscriptions/me/scheduled-change/ — cancel a pending downgrade.

    Idempotent. Releases any active Stripe ``SubscriptionSchedule`` attached
    to the caller's active sub in the resolved context, restoring "no
    pending change" state. Safe to call when no schedule exists — returns
    the current sub unchanged. The corresponding
    ``subscription_schedule.released`` webhook also clears the local mirror;
    the view writes the cleared state up front so the immediate refetch
    reflects it without webhook lag.
    """

    @extend_schema(
        parameters=[_CURRENCY_PARAM, _SUBSCRIPTION_CONTEXT_PARAM],
        request=None,
        responses={
            200: SubscriptionSerializer,
            400: OpenApiResponse(
                description=(
                    "The ``?context=`` query param is set to a value other than"
                    " ``personal``/``team``."
                )
            ),
            403: OpenApiResponse(
                description=(
                    "``?context=team``: caller is missing ``is_billing=True`` on"
                    " their active org membership."
                )
            ),
            404: OpenApiResponse(
                description="No active subscription in the resolved context."
            ),
        },
        description=(
            "Cancel a pending plan-switch (deferred downgrade) on the active"
            " subscription. Idempotent — returns the unchanged subscription"
            " when no schedule exists. Same context-routing and is_billing"
            " gate as PATCH/DELETE on ``/me/``."
        ),
        tags=["billing"],
    )
    def delete(self, request: Request) -> Response:
        user = get_user(request)
        context, _org_id = _resolve_mutation_context(request, user)

        async def _do() -> None:
            customer, _, _ = await _get_customer_and_paid_subscription(user, context=context)
            await release_pending_schedule_for_customer(
                stripe_customer_id=customer.id,
                subscription_repo=get_billing_repos().subscriptions,
            )

        async_to_sync(_do)()
        ctx = _currency_context(request)
        sub = _refetch_subscription_after_mutation(
            user,
            context=context,
            currency=ctx["currency"],
            preferred_currency=ctx.get("preferred_currency"),
        )
        return Response(SubscriptionSerializer(sub, context=ctx).data)


def _refetch_subscription_after_mutation(
    user: User,
    *,
    context: str,
    currency: str = "usd",
    preferred_currency: str | None = None,
) -> SubscriptionModel:
    """Return the (single) sub matching *context* after a PATCH/DELETE round-trip.

    The webhook may not have caught up yet; we want the row our DB knows about
    in this scope, not whichever sub happens to sort newest. Picks the team
    sub for ``context="team"`` (matched on ``stripe_customer.org_id``) and the
    personal sub otherwise. Raises ``NotFound`` if the sub disappeared.

    ``currency`` and ``preferred_currency`` are forwarded to the underlying
    queryset's ``LocalizedPrice`` prefetch so the serialized response can
    render the display + dual-display amounts without firing per-row lookups.
    """
    subs = _get_active_subscriptions_for_user(
        user, currency=currency, preferred_currency=preferred_currency
    )
    if context == _SUBSCRIPTION_CONTEXT_TEAM:
        for sub in subs:
            if sub.stripe_customer is not None and sub.stripe_customer.org_id is not None:
                return sub
    else:
        for sub in subs:
            if sub.stripe_customer is None or sub.stripe_customer.org_id is None:
                return sub
    raise NotFound("No active subscription found.")
