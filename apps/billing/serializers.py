"""Request/response serializers for the billing app."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers
from saasmint_core.services.currency import format_amount

from apps.billing.models import Plan, PlanPrice, Product, ProductPrice, Subscription


def _convert_amount(amount: int, currency: str, rate: float) -> float:
    """Convert a USD-cents amount to a display amount in the target currency."""
    converted = round(amount * rate)
    return format_amount(int(converted), currency)


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
    if allowed_hosts and any(
        hostname == host or (host.startswith(".") and hostname.endswith(host))
        for host in allowed_hosts
        if host != "*"
    ):
        return url

    raise serializers.ValidationError("URL domain is not in the list of allowed origins.")


class PlanPriceSerializer(serializers.ModelSerializer[PlanPrice]):
    display_amount = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = PlanPrice
        fields = ("id", "amount", "display_amount", "currency")
        read_only_fields = ("id", "amount")

    def get_display_amount(self, obj: PlanPrice) -> float:
        return _convert_amount(
            obj.amount,
            self.context.get("currency", "usd"),
            self.context.get("rate", 1.0),
        )

    def get_currency(self, obj: PlanPrice) -> str:
        return str(self.context.get("currency", "usd"))


class PlanSerializer(serializers.ModelSerializer[Plan]):
    price = PlanPriceSerializer(read_only=True)

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


class ProductPriceSerializer(serializers.ModelSerializer[ProductPrice]):
    display_amount = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = ProductPrice
        fields = ("id", "amount", "display_amount", "currency")
        read_only_fields = ("id", "amount")

    def get_display_amount(self, obj: ProductPrice) -> float:
        return _convert_amount(
            obj.amount,
            self.context.get("currency", "usd"),
            self.context.get("rate", 1.0),
        )

    def get_currency(self, obj: ProductPrice) -> str:
        return str(self.context.get("currency", "usd"))


class ProductSerializer(serializers.ModelSerializer[Product]):
    price = ProductPriceSerializer(read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "type", "credits", "is_active", "price")
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer[Subscription]):
    plan = PlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = (
            "id",
            "status",
            "plan",
            "quantity",
            "trial_ends_at",
            "current_period_start",
            "current_period_end",
            "canceled_at",
            "created_at",
        )
        read_only_fields = fields


class CheckoutRequestSerializer(serializers.Serializer[object]):
    plan_price_id = serializers.UUIDField()
    quantity = serializers.IntegerField(default=1, min_value=1, max_value=10000)
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()
    trial_period_days = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=90
    )

    def validate_success_url(self, value: str) -> str:
        return _validate_redirect_url(value)

    def validate_cancel_url(self, value: str) -> str:
        return _validate_redirect_url(value)


class PortalRequestSerializer(serializers.Serializer[object]):
    return_url = serializers.URLField()

    def validate_return_url(self, value: str) -> str:
        return _validate_redirect_url(value)


class UpdateSubscriptionSerializer(serializers.Serializer[object]):
    plan_price_id = serializers.UUIDField(required=False)
    prorate = serializers.BooleanField(default=True)
    quantity = serializers.IntegerField(min_value=1, max_value=10000, required=False)
    cancel_at_period_end = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        has_plan_change = "plan_price_id" in attrs or "quantity" in attrs
        has_cancel_toggle = "cancel_at_period_end" in attrs

        if not has_plan_change and not has_cancel_toggle:
            raise serializers.ValidationError(
                "At least one of 'plan_price_id', 'quantity', or "
                "'cancel_at_period_end' is required."
            )
        # Cancel/resume is a standalone toggle — mixing it with plan/seat
        # changes makes the intent ambiguous (e.g. upgrade-then-cancel).
        # Clients should send two requests instead.
        if has_cancel_toggle and has_plan_change:
            raise serializers.ValidationError(
                "'cancel_at_period_end' cannot be combined with 'plan_price_id' or 'quantity'."
            )
        return attrs
