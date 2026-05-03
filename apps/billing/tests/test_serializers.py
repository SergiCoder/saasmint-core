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
        assert data["display_amount"] == 9.99
        assert data["currency"] == "usd"

    def test_model_fields_read_only(self):
        assert set(PlanPriceSerializer.Meta.read_only_fields) == {"id", "amount"}


@pytest.mark.django_db
class TestPlanSerializer:
    def test_serializes_with_price(self, plan, plan_price):
        data = PlanSerializer(plan).data
        assert data["name"] == "Personal Monthly"
        assert data["context"] == "personal"
        assert data["tier"] == "basic"
        assert data["interval"] == "month"
        assert data["price"]["amount"] == 999

    def test_all_fields_read_only(self):
        assert set(PlanSerializer.Meta.read_only_fields) == set(PlanSerializer.Meta.fields)


@pytest.mark.django_db
class TestSubscriptionSerializer:
    def test_serializes_fields(self, subscription):
        data = SubscriptionSerializer(subscription).data
        assert data["status"] == "active"
        assert data["seat_limit"] == 1
        assert "current_period_start" in data
        assert "current_period_end" in data
        assert "created_at" in data
        # cancel_at is exposed (as None by default) so the frontend can show a
        # precise scheduled-cancel date instead of inferring from period_end.
        assert "cancel_at" in data
        assert data["cancel_at"] is None

    def test_seats_used_personal_subscription_always_one(self, subscription):
        """Personal subs (no org on the stripe_customer) always return 1 for
        ``seats_used`` regardless of anything else."""
        data = SubscriptionSerializer(subscription).data
        assert data["seats_used"] == 1

    def test_seats_used_team_subscription_counts_org_members(self, team_plan, team_plan_price):
        """Team subs (customer has an org) return the live OrgMember count via
        the fallback COUNT query (the annotation path is exercised via views)."""
        from datetime import UTC, datetime

        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="ser-owner@example.com", full_name="Owner")
        member1 = User.objects.create_user(email="ser-m1@example.com", full_name="M1")
        member2 = User.objects.create_user(email="ser-m2@example.com", full_name="M2")
        org = Org.objects.create(name="SerOrg", slug="ser-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER)
        OrgMember.objects.create(org=org, user=member1, role=OrgRole.MEMBER)
        OrgMember.objects.create(org=org, user=member2, role=OrgRole.MEMBER)
        customer = StripeCustomer.objects.create(
            stripe_id="cus_ser_team", org=org, livemode=False
        )
        sub = Subscription.objects.create(
            stripe_id="sub_ser_team",
            stripe_customer=customer,
            status="active",
            plan=team_plan,
            seat_limit=5,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        # Fetch fresh so stripe_customer is available via select_related.
        sub = Subscription.objects.select_related("stripe_customer").get(id=sub.id)
        data = SubscriptionSerializer(sub).data
        # 3 members in the org.
        assert data["seats_used"] == 3

    def test_scheduled_plan_and_change_at_exposed(self, subscription, team_plan):
        """``scheduled_plan`` and ``scheduled_change_at`` must be present in
        the serialized output; they start as None for a plain sub."""
        data = SubscriptionSerializer(subscription).data
        assert "scheduled_plan" in data
        assert "scheduled_change_at" in data
        assert data["scheduled_plan"] is None
        assert data["scheduled_change_at"] is None


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
        settings.CORS_ALLOW_ALL_ORIGINS = False
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

    def test_seat_limit_defaults_to_1(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        ser.is_valid()
        assert ser.validated_data["seat_limit"] == 1

    def test_seat_limit_min_value(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        ser = CheckoutRequestSerializer(
            data={
                "plan_price_id": _PLAN_PRICE_UUID,
                "seat_limit": 0,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            }
        )
        assert not ser.is_valid()
        assert "seat_limit" in ser.errors

    def test_allowed_host_wildcard_excluded(self, settings):
        settings.CORS_ALLOW_ALL_ORIGINS = False
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
        settings.CORS_ALLOW_ALL_ORIGINS = False
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
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 5})
        assert ser.is_valid(), ser.errors

    def test_both_fields(self):
        ser = UpdateSubscriptionSerializer(
            data={"plan_price_id": _PLAN_PRICE_UUID, "seat_limit": 5}
        )
        assert ser.is_valid(), ser.errors

    def test_empty_body_rejected(self):
        ser = UpdateSubscriptionSerializer(data={})
        assert not ser.is_valid()

    def test_invalid_seat_limit(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 0})
        assert not ser.is_valid()
        assert "seat_limit" in ser.errors

    def test_seat_limit_at_min_boundary(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 1})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["seat_limit"] == 1

    def test_negative_seat_limit_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": -1})
        assert not ser.is_valid()
        assert "seat_limit" in ser.errors

    def test_seat_limit_at_max_boundary(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 10000})
        assert ser.is_valid(), ser.errors

    def test_seat_limit_above_max_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 10001})
        assert not ser.is_valid()
        assert "seat_limit" in ser.errors

    def test_only_prorate_without_action_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"prorate": True})
        assert not ser.is_valid()

    def test_cancel_at_period_end_true_alone_valid(self):
        ser = UpdateSubscriptionSerializer(data={"cancel_at_period_end": True})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["cancel_at_period_end"] is True

    def test_cancel_at_period_end_false_alone_valid(self):
        ser = UpdateSubscriptionSerializer(data={"cancel_at_period_end": False})
        assert ser.is_valid(), ser.errors
        assert ser.validated_data["cancel_at_period_end"] is False

    def test_cancel_at_period_end_with_plan_change_rejected(self):
        ser = UpdateSubscriptionSerializer(
            data={"plan_price_id": _PLAN_PRICE_UUID, "cancel_at_period_end": True}
        )
        assert not ser.is_valid()

    def test_cancel_at_period_end_with_seat_limit_rejected(self):
        ser = UpdateSubscriptionSerializer(data={"seat_limit": 3, "cancel_at_period_end": False})
        assert not ser.is_valid()

    def test_both_fields_preserves_values(self):
        ser = UpdateSubscriptionSerializer(
            data={"plan_price_id": _PLAN_PRICE_UUID, "seat_limit": 3, "prorate": False}
        )
        assert ser.is_valid(), ser.errors
        # UUIDField parses the string into a uuid.UUID instance
        assert ser.validated_data["plan_price_id"] == UUID(_PLAN_PRICE_UUID)
        assert ser.validated_data["seat_limit"] == 3
        assert ser.validated_data["prorate"] is False


@pytest.mark.django_db
class TestPlanPriceSerializerCurrency:
    """PlanPriceSerializer with non-USD currency context."""

    def test_converts_amount_with_eur_context(self, plan_price):
        ctx = {"currency": "eur", "rate": 0.91}
        data = PlanPriceSerializer(plan_price, context=ctx).data
        assert data["currency"] == "eur"
        # 999 * 0.91 = 909.09 → round → 909 → /100 → 9.09 → friendly → 8.99
        # (nearest of {8.99, 9.49, 9.99}; 8.99 is 0.10 away)
        assert data["display_amount"] == 8.99
        assert data["approximate"] is True
        assert data["amount"] == 999  # original unchanged

    def test_converts_amount_with_jpy_zero_decimal(self, plan_price):
        ctx = {"currency": "jpy", "rate": 149.5}
        data = PlanPriceSerializer(plan_price, context=ctx).data
        assert data["currency"] == "jpy"
        # 999 * 149.5 = 149350.5 → round → 149350 → zero-decimal → friendly → 149400.0
        assert data["display_amount"] == 149400.0
        assert data["approximate"] is True


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

    def test_model_fields_read_only(self):
        assert set(ProductPriceSerializer.Meta.read_only_fields) == {"id", "amount"}

    def test_converts_amount_with_currency_context(self):
        product = Product.objects.create(
            name="Credits", type="one_time", credits=50, is_active=True
        )
        price = ProductPrice.objects.create(
            product=product, stripe_price_id="price_pp_ctx", amount=500
        )
        ctx = {"currency": "gbp", "rate": 0.79}
        data = ProductPriceSerializer(price, context=ctx).data
        assert data["currency"] == "gbp"
        # 500 * 0.79 = 395 → round → 395 → /100 → 3.95 → friendly → 3.99 (rounds up)
        assert data["display_amount"] == 3.99
        assert data["approximate"] is True


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
