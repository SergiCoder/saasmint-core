"""Request/response serializers for the billing app."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers
from saasmint_core.services.currency import format_amount

from apps.billing.models import Plan, PlanPrice, PlanTier, Product, ProductPrice, Subscription


def _localized_display(
    price: PlanPrice | ProductPrice, currency: str
) -> tuple[float, str]:
    """Return ``(display_amount, effective_currency)`` for ``price``.

    Reads a precomputed ``LocalizedPrice`` row written by the daily
    ``sync_localized_prices`` task. USD always returns the catalog amount
    (the source of truth Stripe charges); any other currency falls back to
    the USD ``amount`` when the localized row is missing (catalog newer
    than the last sync, or sync upstream is down).

    The second element of the tuple is the currency that actually
    denominates ``display_amount`` — callers must use this value for the
    ``currency`` field in the response so the two fields are consistent.
    When a localized row exists the effective currency matches the
    requested one; on fallback it is always ``"usd"``.

    The ``localized_prices`` reverse-relation is expected to be prefetched
    by the calling view — list endpoints attach a ``Prefetch`` filtered to
    the resolved currency, so iterating ``.all()`` here costs no DB hit.
    """
    if currency == "usd":
        return format_amount(price.amount, "usd"), "usd"
    for lp in price.localized_prices.all():
        if lp.currency == currency:
            return format_amount(lp.amount_minor, currency), currency
    return format_amount(price.amount, "usd"), "usd"


def _local_display(
    price: PlanPrice | ProductPrice, preferred_currency: str | None
) -> tuple[float | None, str | None]:
    """Return ``(local_display_amount, local_currency)`` for the dual-display card.

    Populated only when *preferred_currency* is set in the serializer context
    (which the view does **only** when the user's preferred currency is
    non-billable and we therefore fell back to USD for the actual charge).
    For users whose preference is itself billable, the view sets this to
    ``None`` and both elements come back ``None`` — the FE renders the
    standard single-line card.

    Unlike :func:`_localized_display`, no fallback to USD here: a missing
    ``LocalizedPrice`` row means we have no useful local approximation to
    show, and the primary ``display_amount`` already covers the customer's
    actual charge.
    """
    if preferred_currency is None:
        return None, None
    for lp in price.localized_prices.all():
        if lp.currency == preferred_currency:
            return format_amount(lp.amount_minor, preferred_currency), preferred_currency
    return None, None


@functools.cache
def _host_matchers(
    allowed_hosts: tuple[str, ...],
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split ALLOWED_HOSTS into ``(exact_hosts, suffix_hosts)`` for O(1) lookups.

    Cache key is the immutable tuple form of ``ALLOWED_HOSTS`` — tests that
    flip the setting on the fly automatically miss the cache and rebuild,
    so no manual invalidation is needed.
    """
    exact = frozenset(h for h in allowed_hosts if h != "*" and not h.startswith("."))
    suffixes = tuple(h for h in allowed_hosts if h.startswith("."))
    return exact, suffixes


def _validate_redirect_url(url: str) -> str:
    """Ensure a redirect URL belongs to an allowed domain."""
    allowed_origins: list[str] = getattr(settings, "CORS_ALLOWED_ORIGINS", [])
    allowed_hosts: list[str] = getattr(settings, "ALLOWED_HOSTS", [])
    cors_allow_all: bool = getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise serializers.ValidationError("Only HTTP(S) redirect URLs are allowed.")

    # Dev convenience: when CORS is wide open, accept any HTTP(S) origin so
    # local frontends (mkcert localhost, docker network hosts, etc.) work
    # without an explicit allowlist. Prod never enables this flag.
    if cors_allow_all:
        return url

    origin = f"{parsed.scheme}://{parsed.netloc}"
    hostname = parsed.hostname or ""

    if allowed_origins and origin in allowed_origins:
        return url
    if allowed_hosts:
        exact_hosts, suffix_hosts = _host_matchers(tuple(allowed_hosts))
        if hostname in exact_hosts:
            return url
        if any(hostname.endswith(s) for s in suffix_hosts):
            return url

    raise serializers.ValidationError("URL domain is not in the list of allowed origins.")


class _PriceSerializer(serializers.ModelSerializer[Any]):
    """Shared base for PlanPrice / ProductPrice serializers.

    Declaring the two display-currency fields and their getters once on a
    ModelSerializer base lets concrete subclasses supply only the Meta.model
    binding. DRF's metaclass walks `_declared_fields` on base Serializer
    classes (unlike plain mixins), so the fields flow through inheritance.

    Not instantiated directly: Meta.model is left unset so subclass Metas can
    inject the concrete model.
    """

    display_amount = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    local_display_amount = serializers.SerializerMethodField()
    local_currency = serializers.SerializerMethodField()

    if TYPE_CHECKING:
        context: dict[str, Any]

    class Meta:
        fields = (
            "id",
            "amount",
            "display_amount",
            "currency",
            "local_display_amount",
            "local_currency",
        )
        read_only_fields = ("id", "amount")

    def to_representation(self, instance: PlanPrice | ProductPrice) -> dict[str, Any]:
        # Compute the localized + local tuples once per instance and stash
        # them so the four SerializerMethodField getters below read from a
        # cache instead of recomputing _localized_display / _local_display
        # twice per row (each call walks ``localized_prices.all()``).
        instance._display_tuple = _localized_display(  # type: ignore[union-attr]
            instance, self.context.get("currency", "usd")
        )
        instance._local_tuple = _local_display(  # type: ignore[union-attr]
            instance, self.context.get("preferred_currency")
        )
        return super().to_representation(instance)

    def get_display_amount(self, obj: PlanPrice | ProductPrice) -> float:
        amount: float = obj._display_tuple[0]  # type: ignore[union-attr]  # populated in to_representation
        return amount

    def get_currency(self, obj: PlanPrice | ProductPrice) -> str:
        currency: str = obj._display_tuple[1]  # type: ignore[union-attr]  # populated in to_representation
        return currency

    def get_local_display_amount(self, obj: PlanPrice | ProductPrice) -> float | None:
        amount: float | None = obj._local_tuple[0]  # type: ignore[union-attr]  # populated in to_representation
        return amount

    def get_local_currency(self, obj: PlanPrice | ProductPrice) -> str | None:
        currency: str | None = obj._local_tuple[1]  # type: ignore[union-attr]  # populated in to_representation
        return currency


class PlanPriceSerializer(_PriceSerializer):
    class Meta(_PriceSerializer.Meta):
        model = PlanPrice


class PlanSerializer(serializers.ModelSerializer[Plan]):
    price = PlanPriceSerializer(read_only=True)
    # Expose the tier as its string label (``"free"``/``"basic"``/``"pro"``) to
    # stay consistent with every other enum on the wire (``context``,
    # ``interval``, ``status``, ``role``) — clients otherwise have to
    # special-case an int for this one field.
    tier = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "id",
            "name",
            "description",
            "context",
            "tier",
            "interval",
            "is_active",
            "price",
        )
        read_only_fields = fields

    def get_tier(self, obj: Plan) -> str:
        return PlanTier(obj.tier).label.lower()


class ProductPriceSerializer(_PriceSerializer):
    class Meta(_PriceSerializer.Meta):
        model = ProductPrice


class ProductSerializer(serializers.ModelSerializer[Product]):
    price = ProductPriceSerializer(read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "type", "credits", "is_active", "price")
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer[Subscription]):
    plan = PlanSerializer(read_only=True)
    scheduled_plan = PlanSerializer(read_only=True)
    seats_used = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id",
            "status",
            "plan",
            "seat_limit",
            "seats_used",
            "trial_ends_at",
            "current_period_start",
            "current_period_end",
            "canceled_at",
            "cancel_at",
            "scheduled_plan",
            "scheduled_change_at",
            "currency",
            "created_at",
        )
        read_only_fields = fields

    def get_seats_used(self, obj: Subscription) -> int:
        """Number of seats currently occupied.

        Always 1 for personal subscriptions. For team subscriptions,
        reads the ``org_member_count`` annotation attached by
        ``_get_active_subscriptions_for_user`` — the only path that
        serialises team subs through this serializer in production.
        Raises if the annotation is missing rather than firing a per-row
        COUNT query, so a future caller that drops the annotation gets a
        loud error instead of a silent N+1.
        """
        org_id = getattr(obj.stripe_customer, "org_id", None) if obj.stripe_customer_id else None
        if org_id is None:
            return 1
        annotated: int | None = getattr(obj, "org_member_count", None)
        if annotated is None:
            raise RuntimeError(
                "SubscriptionSerializer requires the org_member_count annotation "
                "for team subscriptions — call _get_active_subscriptions_for_user "
                "or annotate the queryset before serialisation."
            )
        return annotated


class CheckoutRequestSerializer(serializers.Serializer[object]):
    plan_price_id = serializers.UUIDField()
    seat_limit = serializers.IntegerField(default=1, min_value=1, max_value=10000)
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()
    trial_period_days = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=90
    )
    org_name = serializers.CharField(max_length=255, required=False)
    keep_personal_subscription = serializers.BooleanField(default=False)

    def validate_success_url(self, value: str) -> str:
        return _validate_redirect_url(value)

    def validate_cancel_url(self, value: str) -> str:
        return _validate_redirect_url(value)


class PortalRequestSerializer(serializers.Serializer[object]):
    return_url = serializers.URLField()

    def validate_return_url(self, value: str) -> str:
        return _validate_redirect_url(value)


class ProductCheckoutRequestSerializer(serializers.Serializer[object]):
    product_price_id = serializers.UUIDField()
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()

    def validate_success_url(self, value: str) -> str:
        return _validate_redirect_url(value)

    def validate_cancel_url(self, value: str) -> str:
        return _validate_redirect_url(value)


class CreditBalanceEntrySerializer(serializers.Serializer[object]):
    balance = serializers.IntegerField(read_only=True)
    scope = serializers.CharField(read_only=True)


class CreditBalanceSerializer(serializers.Serializer[object]):
    balances = CreditBalanceEntrySerializer(many=True, read_only=True)


class UpdateSubscriptionSerializer(serializers.Serializer[object]):
    plan_price_id = serializers.UUIDField(required=False)
    prorate = serializers.BooleanField(default=True)
    seat_limit = serializers.IntegerField(min_value=1, max_value=10000, required=False)
    cancel_at_period_end = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        has_plan_change = "plan_price_id" in attrs or "seat_limit" in attrs
        has_cancel_toggle = "cancel_at_period_end" in attrs

        if not has_plan_change and not has_cancel_toggle:
            raise serializers.ValidationError(
                "At least one of 'plan_price_id', 'seat_limit', or "
                "'cancel_at_period_end' is required."
            )
        # Cancel/resume is a standalone toggle — mixing it with plan/seat
        # changes makes the intent ambiguous (e.g. upgrade-then-cancel).
        # Clients should send two requests instead.
        if has_cancel_toggle and has_plan_change:
            raise serializers.ValidationError(
                "'cancel_at_period_end' cannot be combined with 'plan_price_id' or 'seat_limit'."
            )
        return attrs
