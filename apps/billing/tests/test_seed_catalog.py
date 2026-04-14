"""Tests for the seed_catalog management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.billing.models import (
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    PlanTier,
    Product,
    ProductPrice,
    ProductType,
)


@pytest.mark.django_db
class TestSeedCatalogPlans:
    def test_creates_all_plans(self):
        call_command("seed_catalog", stdout=StringIO())
        # 5 monthly (personal free/basic/pro + team basic/pro)
        # + 4 yearly (paid only, no free yearly)
        assert Plan.objects.count() == 9

    def test_creates_personal_free_monthly_plan(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.PERSONAL,
            tier=PlanTier.FREE,
            interval=PlanInterval.MONTH,
        )
        assert plan.is_active
        assert plan.name == "Personal Free"

    def test_creates_team_pro_yearly_plan(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.TEAM,
            tier=PlanTier.PRO,
            interval=PlanInterval.YEAR,
        )
        assert plan.is_active

    def test_no_free_yearly_plan(self):
        call_command("seed_catalog", stdout=StringIO())
        assert not Plan.objects.filter(
            tier=PlanTier.FREE,
            interval=PlanInterval.YEAR,
        ).exists()


@pytest.mark.django_db
class TestSeedCatalogPrices:
    def test_creates_price_per_plan(self):
        call_command("seed_catalog", stdout=StringIO())
        assert PlanPrice.objects.count() == 9

    def test_free_plan_has_zero_amount(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.PERSONAL,
            tier=PlanTier.FREE,
            interval=PlanInterval.MONTH,
        )
        assert plan.price.amount == 0

    def test_personal_pro_monthly_amount(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.PERSONAL,
            tier=PlanTier.PRO,
            interval=PlanInterval.MONTH,
        )
        assert plan.price.amount == 4900

    def test_team_basic_yearly_amount(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.TEAM,
            tier=PlanTier.BASIC,
            interval=PlanInterval.YEAR,
        )
        assert plan.price.amount == 17000

    def test_price_has_placeholder_stripe_id(self):
        call_command("seed_catalog", stdout=StringIO())
        plan = Plan.objects.get(
            context=PlanContext.PERSONAL,
            tier=PlanTier.PRO,
            interval=PlanInterval.MONTH,
        )
        assert plan.price.stripe_price_id.startswith("price_placeholder_")


@pytest.mark.django_db
class TestSeedCatalogProducts:
    def test_creates_boost_products(self):
        call_command("seed_catalog", stdout=StringIO())
        assert Product.objects.count() == 3
        assert Product.objects.filter(name="50 Credits").exists()
        assert Product.objects.filter(name="200 Credits").exists()
        assert Product.objects.filter(name="500 Credits").exists()

    def test_products_are_one_time_type(self):
        call_command("seed_catalog", stdout=StringIO())
        product = Product.objects.get(name="50 Credits")
        assert product.type == ProductType.ONE_TIME
        assert product.is_active

    def test_products_have_credit_counts(self):
        call_command("seed_catalog", stdout=StringIO())
        assert Product.objects.get(name="50 Credits").credits == 50
        assert Product.objects.get(name="200 Credits").credits == 200
        assert Product.objects.get(name="500 Credits").credits == 500

    def test_creates_product_prices(self):
        call_command("seed_catalog", stdout=StringIO())
        assert ProductPrice.objects.count() == 3
        price = ProductPrice.objects.get(product__name="50 Credits")
        assert price.amount == 499
        assert price.stripe_price_id == "price_placeholder_boost_50"


@pytest.mark.django_db
class TestSeedCatalogIdempotency:
    def test_running_twice_does_not_duplicate_plans(self):
        call_command("seed_catalog", stdout=StringIO())
        first = Plan.objects.count()
        call_command("seed_catalog", stdout=StringIO())
        assert Plan.objects.count() == first

    def test_running_twice_does_not_duplicate_plan_prices(self):
        call_command("seed_catalog", stdout=StringIO())
        first = PlanPrice.objects.count()
        call_command("seed_catalog", stdout=StringIO())
        assert PlanPrice.objects.count() == first

    def test_running_twice_does_not_duplicate_products(self):
        call_command("seed_catalog", stdout=StringIO())
        first = Product.objects.count()
        call_command("seed_catalog", stdout=StringIO())
        assert Product.objects.count() == first

    def test_running_twice_does_not_duplicate_product_prices(self):
        call_command("seed_catalog", stdout=StringIO())
        first = ProductPrice.objects.count()
        call_command("seed_catalog", stdout=StringIO())
        assert ProductPrice.objects.count() == first

    def test_reports_success(self):
        out = StringIO()
        call_command("seed_catalog", stdout=out)
        assert "Catalog seeded" in out.getvalue()
