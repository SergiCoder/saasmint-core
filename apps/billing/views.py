"""Billing API views — checkout, portal, subscriptions."""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync
from django.core.cache import cache
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
from saasmint_core.services.billing import (
    cancel_subscription,
    create_billing_portal_session,
    create_checkout_session,
    create_product_checkout_session,
    create_team_stripe_customer,
    get_or_create_customer,
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
    ExchangeRate,
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
from apps.users.models import AccountType, User
from helpers import get_user

logger = logging.getLogger(__name__)

MIN_TEAM_SEATS = 1


class _AccountTypeMismatch(APIException):
    """409 — caller is not eligible for the requested plan context.

    Covers two cases that share the same ``account_type_mismatch`` code:
    an ORG account checking out a personal plan, and a user who already
    owns an org trying to start a second team checkout (rule 8). The
    PERSONAL→team upgrade path (rule 16) does not raise this — a PERSONAL
    user without an owned org is allowed through and gets flipped to
    ORG_MEMBER inside ``_create_org_with_owner``.

    Raising a bare ``ValidationError({"detail": "..."})`` would coerce the
    string into a list (``{"detail": ["..."]}``) and escape past the custom
    exception middleware, leaking DRF's internal shape. A typed
    ``APIException`` keeps the envelope flat and carries a stable ``code``.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Account type does not match the plan's context."
    default_code = "account_type_mismatch"


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
        "``personal`` or ``team``. Defaults to ``team`` for ``ORG_MEMBER`` "
        "callers and ``personal`` otherwise. Ignored on GET — that endpoint "
        "always returns every active subscription the caller can see."
    ),
    required=False,
    type=str,
    enum=["personal", "team"],
)


def _resolve_display_currency(
    query_currency: str | None,
    user: User | None,
) -> str:
    """Resolve the display currency.

    Priority: explicit query param → ``user.preferred_currency`` (if any) → USD.
    """
    if query_currency is not None and query_currency != "":
        qp = query_currency.lower()
        if qp not in SUPPORTED_CURRENCIES:
            raise ValidationError({"currency": [f"Unsupported currency: {query_currency!r}."]})
        return qp

    if user is not None:
        preferred = user.preferred_currency
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
    user: User | None = request.user if request.user.is_authenticated else None
    resolved = _resolve_display_currency(request.query_params.get("currency"), user)
    currency, rate = _get_exchange_rate(resolved)
    return {"currency": currency, "rate": rate}


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


def _default_subscription_context(user: User) -> str:
    """Resolve the default ``?context=`` for PATCH/DELETE on /me/.

    ORG_MEMBER → team (existing behavior), PERSONAL → personal. The default
    keeps single-sub callers working unchanged. Concurrent users (rule 5b)
    pass an explicit ``?context=personal`` to manage their personal sub.
    """
    return (
        _SUBSCRIPTION_CONTEXT_TEAM
        if user.account_type == AccountType.ORG_MEMBER
        else _SUBSCRIPTION_CONTEXT_PERSONAL
    )


async def _resolve_billing_customer(
    user: User, *, context: str | None = None
) -> StripeCustomer | None:
    """Return the StripeCustomer that owns billing for *user*, or None.

    Without ``context``: PERSONAL users get their user-scoped customer,
    ORG_MEMBER users get the customer attached to the active org they
    belong to. With ``context="personal"`` or ``"team"``, returns the
    matching customer regardless of ``account_type`` — required for the
    concurrent-billing case (rule 5a/5b) where an ORG_MEMBER user still
    has an active personal sub on their user-scoped customer.
    """
    repos = get_billing_repos()
    effective = context or _default_subscription_context(user)
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
        # PERSONAL users can upgrade to a team plan via team-context checkout
        # (see CheckoutSessionView), so hiding team plans from them would make
        # the upgrade undiscoverable.
        qs = PlanModel.objects.filter(is_active=True).select_related("price")
        data = PlanSerializer(qs, many=True, context=_currency_context(request)).data
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
        products = ProductModel.objects.filter(is_active=True).select_related("price")
        data = ProductSerializer(products, many=True, context=_currency_context(request)).data
        return Response(_catalog_envelope(list(data)))


class CheckoutSessionView(BillingScopedView):
    """POST /api/v1/billing/checkout-sessions — create a Stripe Checkout Session."""

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={
            200: inline_serializer("CheckoutResponse", {"url": drf_serializers.URLField()}),
            400: OpenApiResponse(
                description=(
                    "Request body failed validation (e.g. ``org_name`` missing for a"
                    " team-context plan, invalid quantity for the plan's context)."
                )
            ),
            404: OpenApiResponse(description="Invalid plan price."),
            409: OpenApiResponse(
                description=(
                    "Caller is not eligible for the requested plan context"
                    " (``code=account_type_mismatch``). Two cases share this code:"
                    " an ORG account checking out a personal plan, and a user who"
                    " already owns an organization trying to start a second team"
                    " checkout (rule 8)."
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
        quantity = _validate_quantity_for_plan(plan_price, data["quantity"])

        is_team = plan_price.plan.context == PlanContext.TEAM

        # Personal-context checkouts: caller must be a PERSONAL user. Org members
        # checkout team plans for the org via the team path below.
        if not is_team and user.account_type != AccountType.PERSONAL:
            raise _AccountTypeMismatch("Org accounts cannot check out personal plans.")

        # Team plans: any user without an owned org may upgrade. The PERSONAL→
        # team upgrade flow flips ``account_type`` to ORG_MEMBER atomically with
        # org creation in ``_create_org_with_owner`` (rule 8: one owned org per
        # user). ORG_MEMBER users without an owned org are the legacy
        # /auth/register/org-owner/ path and are allowed for the same reason.
        if is_team:
            from apps.orgs.models import OrgMember, OrgRole

            already_owns_org = OrgMember.objects.filter(
                user_id=user.id, role=OrgRole.OWNER
            ).exists()
            if already_owns_org:
                raise _AccountTypeMismatch("You already own an organization.")

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


class PortalSessionView(BillingScopedView):
    """POST /api/v1/billing/portal-sessions — create a Stripe Customer Portal session."""

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
                customer_repo=get_billing_repos().customers,
            )
            return await create_billing_portal_session(
                stripe_customer_id=customer.stripe_id,
                locale=user.preferred_locale,
                return_url=ser.validated_data["return_url"],
            )

        url = async_to_sync(_do)()
        return Response({"url": url})


def _require_owner_for_product_purchase(user: User) -> UUID | None:
    """Authorize a credit purchase. Returns ``org_id`` for team buys, ``None`` for personal.

    PERSONAL: always allowed, credits go to the user's own balance.
    ORG_MEMBER: must hold ``role=OWNER`` on an active org; admin/member → 403.
    Unlike subscription mutations (which gate on ``is_billing``), credit
    purchases are owner-only — the spend authority sits with the principal,
    not the delegated billing contact.
    """
    if user.account_type != AccountType.ORG_MEMBER:
        return None

    from apps.orgs.models import OrgMember, OrgRole

    owner = (
        OrgMember.objects.filter(
            user_id=user.id,
            role=OrgRole.OWNER,
        )
        .only("org_id")
        .first()
    )
    if owner is None:
        raise PermissionDenied("Only the org owner can purchase credits for the team.")
    return owner.org_id


class ProductCheckoutSessionView(BillingScopedView):
    """POST /api/v1/billing/product-checkout-sessions/ — one-time product purchase."""

    @extend_schema(
        request=ProductCheckoutRequestSerializer,
        responses={
            200: inline_serializer("ProductCheckoutResponse", {"url": drf_serializers.URLField()}),
            403: OpenApiResponse(
                description=("Caller is an ORG_MEMBER without ``role=OWNER`` on their active org.")
            ),
            404: OpenApiResponse(description="Invalid product price."),
        },
        description=(
            "Create a Stripe Checkout Session (``mode=payment``) for a one-time"
            " product purchase (credit pack). PERSONAL users buy for themselves;"
            " ORG_MEMBER owners buy for the org. Admins and regular members are"
            " rejected with 403."
        ),
        tags=["billing"],
    )
    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = ProductCheckoutRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        product_price = _get_active_product_price(data["product_price_id"])
        org_id = _require_owner_for_product_purchase(user)

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
                price_id=product_price.stripe_price_id,
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
            "Return the caller's current credit balance. PERSONAL users see their"
            " own balance; ORG_MEMBER users see their org's balance (readable by"
            " any active member)."
        ),
        tags=["billing"],
    )
    def get(self, request: Request) -> Response:
        user = get_user(request)

        if user.account_type == AccountType.ORG_MEMBER:
            from apps.orgs.models import OrgMember

            # Fetch only the org_id — get_credit_balance filters by FK, so we
            # don't need to hydrate the full Org row via select_related.
            org_id = (
                OrgMember.objects.filter(
                    user_id=user.id,
                )
                .values_list("org_id", flat=True)
                .first()
            )
            if org_id is None:
                raise NotFound("No active org found.")
            balance = get_credit_balance(org_id=org_id)
            scope = "org"
        else:
            balance = get_credit_balance(user=user)
            scope = "user"

        return Response(CreditBalanceSerializer({"balance": balance, "scope": scope}).data)


def _get_active_subscriptions_for_user(user: User) -> list[SubscriptionModel]:
    """Return every active subscription the user has billing visibility into.

    A user can hold up to two concurrent active subscriptions (rules 5a/5b
    + 16):
      - The **team** sub on their org's Stripe customer (any member of the
        active org sees it; the is_billing gate applies to mutations only).
      - The **personal** sub on their own user-scoped Stripe customer
        (PERSONAL users always have at most this one; ORG_MEMBER users may
        also retain one when they keep personal running concurrently).

    Returns 0, 1, or 2 subs (team first when present, then personal). An
    empty list is the new free-tier shape (replaces the old NotFound 404 on
    ``GET /me/``).
    """
    # ``stripe_customer`` is select_related so ``_refetch_subscription_after_mutation``
    # can discriminate team vs personal subs via ``sub.stripe_customer.org_id``
    # without firing an FK lookup per sub.
    base = SubscriptionModel.objects.select_related("plan__price", "stripe_customer").filter(
        status__in=ACTIVE_SUBSCRIPTION_STATUSES
    )
    subs: list[SubscriptionModel] = []
    seen_ids: set[UUID] = set()

    if user.account_type == AccountType.ORG_MEMBER:
        from apps.orgs.models import OrgMember

        membership = (
            OrgMember.objects.filter(
                user_id=user.id,
            )
            .only("org_id")
            .first()
        )
        if membership is not None:
            team_sub = (
                base.filter(stripe_customer__org_id=membership.org_id)
                .order_by("-created_at")
                .first()
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

    For ``context="team"``: requires ORG_MEMBER + ``is_billing=True`` on the
    active membership. Returns the ``org_id`` (used to address the
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
    """
    context = _validate_subscription_context(
        request.query_params.get("context")
    ) or _default_subscription_context(user)
    org_id = _require_billing_authority(user, context=context)
    return context, org_id


def _billing_notice_recipients(user: User, org_id: UUID | None) -> list[str]:
    """Return the list of emails to notify on a billing-state change.

    PERSONAL subs: just the owner. Team subs: every ``is_billing=True`` member
    of the org (so a rogue billing contact's action is visible to peers).
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
        subs = _get_active_subscriptions_for_user(user)
        data = SubscriptionSerializer(subs, many=True, context=_currency_context(request)).data
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

        if plan_price and "quantity" in data:
            _validate_quantity_for_plan(plan_price, data["quantity"])

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
                current_plan = await PlanModel.objects.only("context").aget(id=sub.plan_id)
                _validate_quantity_for_context(PlanContext(current_plan.context), data["quantity"])
                await update_seat_count(
                    stripe_subscription_id=stripe_sub_id,
                    quantity=data["quantity"],
                )

        async_to_sync(_do)()
        sub = _refetch_subscription_after_mutation(user, context=context)
        if "cancel_at_period_end" in data:
            recipients = _billing_notice_recipients(user, org_id)
            if recipients:
                send_subscription_cancel_notice_task.delay(
                    recipients,
                    sub.plan.name,
                    "scheduled" if data["cancel_at_period_end"] else "resumed",
                )
        return Response(SubscriptionSerializer(sub, context=_currency_context(request)).data)

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
        sub = _refetch_subscription_after_mutation(user, context=context)
        recipients = _billing_notice_recipients(user, org_id)
        if recipients:
            send_subscription_cancel_notice_task.delay(recipients, sub.plan.name, "scheduled")
        return Response(
            SubscriptionSerializer(sub, context=_currency_context(request)).data,
            status=status.HTTP_202_ACCEPTED,
        )


def _refetch_subscription_after_mutation(user: User, *, context: str) -> SubscriptionModel:
    """Return the (single) sub matching *context* after a PATCH/DELETE round-trip.

    The webhook may not have caught up yet; we want the row our DB knows about
    in this scope, not whichever sub happens to sort newest. Picks the team
    sub for ``context="team"`` (matched on ``stripe_customer.org_id``) and the
    personal sub otherwise. Raises ``NotFound`` if the sub disappeared.
    """
    subs = _get_active_subscriptions_for_user(user)
    if context == _SUBSCRIPTION_CONTEXT_TEAM:
        for sub in subs:
            if sub.stripe_customer is not None and sub.stripe_customer.org_id is not None:
                return sub
    else:
        for sub in subs:
            if sub.stripe_customer is None or sub.stripe_customer.org_id is None:
                return sub
    raise NotFound("No active subscription found.")
