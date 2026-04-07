"""Request/response serializers for the billing app."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers

from apps.billing.models import Plan, PlanPrice, Product, ProductPrice, Subscription


def _validate_redirect_url(url: str) -> str:
    """Ensure a redirect URL belongs to an allowed domain."""
    allowed_origins: list[str] = getattr(settings, "CORS_ALLOWED_ORIGINS", [])
    allowed_hosts: list[str] = getattr(settings, "ALLOWED_HOSTS", [])

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise serializers.ValidationError("Only HTTP(S) redirect URLs are allowed.")

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
    class Meta:
        model = PlanPrice
        fields = ("id", "amount")
        read_only_fields = fields


class PlanSerializer(serializers.ModelSerializer[Plan]):
    price = PlanPriceSerializer(read_only=True)

    class Meta:
        model = Plan
        fields = ("id", "name", "description", "context", "interval", "is_active", "price")
        read_only_fields = fields


class ProductPriceSerializer(serializers.ModelSerializer[ProductPrice]):
    class Meta:
        model = ProductPrice
        fields = ("id", "amount")
        read_only_fields = fields


class ProductSerializer(serializers.ModelSerializer[Product]):
    price = ProductPriceSerializer(read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "type", "credits", "is_active", "price")
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer[Subscription]):
    class Meta:
        model = Subscription
        fields = (
            "id",
            "status",
            "plan",
            "quantity",
            "discount_percent",
            "discount_end_at",
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
    promo_code = serializers.CharField(
        required=False, allow_null=True, default=None, max_length=255
    )
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

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if "plan_price_id" not in attrs and "quantity" not in attrs:
            raise serializers.ValidationError(
                "At least one of 'plan_price_id' or 'quantity' is required."
            )
        return attrs


class PromoCodeSerializer(serializers.Serializer[object]):
    promo_code = serializers.CharField(max_length=255)
