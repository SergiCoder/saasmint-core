"""Tests for billing serializers."""

from __future__ import annotations

from uuid import UUID

import pytest

from apps.billing.models import Product, ProductPrice
from apps.billing.serializers import (
    CheckoutRequestSerializer,
    PlanPriceSerializer,
    PlanSerializer,
    PortalRequestSerializer,
    ProductPriceSerializer,
    ProductSerializer,
    PromoCodeSerializer,
    SubscriptionSerializer,
    UpdateSubscriptionSerializer,
)

# Sample valid UUID for serializer-level tests that don't need a real DB row.
# UUIDField only validates format, not existence.
_PLAN_PRICE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.django_db
class TestPlanPriceSerializer:
    def test_serializes_fields(self, plan_price):
        data = PlanPriceSerializer(plan_price).data
        assert data["id"] == str(plan_price.id)
        assert data["amount"] == 999

    def test_all_fields_read_only(self):
        assert set(PlanPriceSerializer.Meta.read_only_fields) == set(
            PlanPriceSerializer.Meta.fields
        )


@pytest.mark.django_db
class TestPlanSerializer:
    def test_serializes_with_price(self, plan, plan_price):
        data = PlanSerializer(plan).data
        assert data["name"] == "Personal Monthly"
        assert data["context"] == "personal"
        assert data["interval"] == "month"
        assert data["price"]["amount"] == 999

    def test_all_fields_read_only(self):
        assert set(PlanSerializer.Meta.read_only_fields) == set(PlanSerializer.Meta.fields)


@pytest.mark.django_db
class TestSubscriptionSerializer:
    def test_serializes_fields(self, subscription):
        data = SubscriptionSerializer(subscription).data
        assert data["status"] == "active"
        assert data["quantity"] == 1
        assert "current_period_start" in data
        assert "current_period_end" in data
        assert "created_at" in data


class TestCheckoutRequestSerializer:
    def test_valid_data(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert ser.is_valid(), ser.errors

    def test_missing_required_fields(self):
        ser = CheckoutRequestSerializer(data={})
        assert not ser.is_valid()
        assert "plan_price_id" in ser.errors
        assert "success_url" in ser.errors
        assert "cancel_url" in ser.errors

    def test_invalid_redirect_url_rejected(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        settings.ALLOWED_HOSTS = ["example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://evil.com/phish",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert not ser.is_valid()
        assert "success_url" in ser.errors

    def test_non_http_scheme_rejected(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "javascript://example.com/xss",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert not ser.is_valid()

    def test_quantity_defaults_to_1(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        ser.is_valid()
        assert ser.validated_data["quantity"] == 1

    def test_quantity_min_value(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "quantity": 0,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert not ser.is_valid()
        assert "quantity" in ser.errors

    def test_promo_code_optional(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        ser.is_valid()
        assert ser.validated_data["promo_code"] is None

    def test_allowed_host_wildcard_excluded(self, settings):
        settings.CORS_ALLOWED_ORIGINS = []
        settings.ALLOWED_HOSTS = ["*"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://evil.com/phish",
                "cancel_url": "https://evil.com/cancel",
            }
        )
        assert not ser.is_valid()

    def test_allowed_host_subdomain_match(self, settings):
        settings.CORS_ALLOWED_ORIGINS = []
        settings.ALLOWED_HOSTS = [".example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://app.example.com/success",
                "cancel_url": "https://app.example.com/cancel",
            }
        )
        assert ser.is_valid(), ser.errors

    def test_malformed_plan_price_id_rejected(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": "not-a-uuid",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert not ser.is_valid()
        assert "plan_price_id" in ser.errors


class TestPortalRequestSerializer:
    def test_valid_data(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = PortalRequestSerializer(data={"return_url": "https://example.com/dashboard"})
        assert ser.is_valid(), ser.errors

    def test_missing_return_url(self):
        ser = PortalRequestSerializer(data={})
        assert not ser.is_valid()
        assert "return_url" in ser.errors

    def test_invalid_domain_rejected(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        settings.ALLOWED_HOSTS = ["example.com"]
        ser = PortalRequestSerializer(data={"return_url": "https://evil.com/portal"})
        assert not ser.is_valid()


class TestUpdateSubscriptionSerializer:
    def test_valid_plan_change(self):
        ser = UpdateSubscriptionSerializer(data={"plan_price_id": _PLAN_PRICE_UUID})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["prorate"] is True

    def test_prorate_false(self):
        ser = UpdateSubscriptionSerializer(
            data={"plan_price_id": _PLAN_PRICE_UUID, "prorate": False}
        )
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["prorate"] is False

    def test_valid_seat_update(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": 5})
        assert ser.is_valid(), ser.errors

    def test_both_fields(self):
        ser = UpdateSubscriptionSerializer(data={"plan_price_id": _PLAN_PRICE_UUID, "quantity": 5})
        assert ser.is_valid(), ser.errors

    def test_empty_body_rejected(self):
        ser = UpdateSubscriptionSerializer(data={})
        assert not ser.is_valid()

    def test_invalid_quantity(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": 0})
        assert not ser.is_valid()
        assert "quantity" in ser.errors

    def test_quantity_at_min_boundary(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": 1})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["quantity"] == 1

    def test_negative_quantity_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": -1})
        assert not ser.is_valid()
        assert "quantity" in ser.errors

    def test_quantity_at_max_boundary(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": 10000})
        assert ser.is_valid(), ser.errors

    def test_quantity_above_max_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"quantity": 10001})
        assert not ser.is_valid()
        assert "quantity" in ser.errors

    def test_only_prorate_without_action_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"prorate": True})
        assert not ser.is_valid()

    def test_both_fields_preserves_values(self):
        ser = UpdateSubscriptionSerializer(
            data={"plan_price_id": _PLAN_PRICE_UUID, "quantity": 3, "prorate": False}
        )
        assert ser.is_valid(), ser.errors
        # UUIDField parses the string into a uuid.UUID instance
        assert ser.validated_data["plan_price_id"] == UUID(_PLAN_PRICE_UUID)
        assert ser.validated_data["quantity"] == 3
        assert ser.validated_data["prorate"] is False


class TestPromoCodeSerializer:
    def test_valid(self):
        ser = PromoCodeSerializer(data={"promo_code": "SAVE20"})
        assert ser.is_valid(), ser.errors

    def test_missing(self):
        ser = PromoCodeSerializer(data={})
        assert not ser.is_valid()


@pytest.mark.django_db
class TestProductPriceSerializer:
    def test_serializes_fields(self):
        product = Product.objects.create(
            name="100 Credits", type="one_time", credits=100, is_active=True
        )
        price = ProductPrice.objects.create(
            product=product, stripe_price_id="price_pp_1", amount=999
        )
        data = ProductPriceSerializer(price).data
        assert data["id"] == str(price.id)
        assert data["amount"] == 999

    def test_all_fields_read_only(self):
        assert set(ProductPriceSerializer.Meta.read_only_fields) == set(
            ProductPriceSerializer.Meta.fields
        )


@pytest.mark.django_db
class TestProductSerializer:
    def test_serializes_with_price(self):
        product = Product.objects.create(
            name="500 Credits", type="one_time", credits=500, is_active=True
        )
        ProductPrice.objects.create(product=product, stripe_price_id="price_pp_2", amount=4999)
        data = ProductSerializer(product).data
        assert data["name"] == "500 Credits"
        assert data["type"] == "one_time"
        assert data["credits"] == 500
        assert data["is_active"] is True
        assert data["price"]["amount"] == 4999

    def test_all_fields_read_only(self):
        assert set(ProductSerializer.Meta.read_only_fields) == set(ProductSerializer.Meta.fields)
