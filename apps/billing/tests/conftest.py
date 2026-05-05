"""Shared fixtures for the billing test package."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from django.core.cache import cache

from apps.billing.models import Plan, PlanPrice, Product, ProductPrice, StripeCustomer, Subscription
from apps.users.models import User

# ---------------------------------------------------------------------------
# Shared helper functions (not fixtures — call directly in tests)
# ---------------------------------------------------------------------------


def fx_response(rates: dict[str, float]) -> MagicMock:
    """Build a mock httpx.Response in the open.er-api.com success shape."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "result": "success",
        "rates": {k.upper(): v for k, v in rates.items()},
    }
    return resp


def seed_plan_price(amount: int = 999) -> PlanPrice:
    """Create a Plan + PlanPrice row for use in tests that need a catalog entry."""
    plan = Plan.objects.create(
        name="Pro Monthly", context="personal", tier=3, interval="month"
    )
    return PlanPrice.objects.create(plan=plan, stripe_price_id=f"price_{plan.id}", amount=amount)


def seed_product_price(amount: int = 1500) -> ProductPrice:
    """Create a Product + ProductPrice row for use in tests that need a catalog entry."""
    product = Product.objects.create(name="Boost", type="one_time", credits=100)
    return ProductPrice.objects.create(
        product=product, stripe_price_id=f"price_{product.id}", amount=amount
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="billing@example.com",
        full_name="Billing User",
    )


@pytest.fixture
def plan(db):
    return Plan.objects.create(
        name="Personal Monthly",
        context="personal",
        interval="month",
        is_active=True,
    )


@pytest.fixture
def plan_price(plan):
    return PlanPrice.objects.create(
        plan=plan,
        stripe_price_id="price_test_123",
        amount=999,
    )


@pytest.fixture
def team_plan(db):
    return Plan.objects.create(
        name="Team Monthly",
        context="team",
        interval="month",
        is_active=True,
    )


@pytest.fixture
def team_plan_price(team_plan):
    return PlanPrice.objects.create(
        plan=team_plan,
        stripe_price_id="price_team_123",
        amount=1500,
    )


@pytest.fixture
def stripe_customer(user):
    return StripeCustomer.objects.create(
        stripe_id="cus_test_123",
        user=user,
        livemode=False,
    )


@pytest.fixture
def subscription(stripe_customer, plan, plan_price):
    return Subscription.objects.create(
        stripe_id="sub_test_123",
        stripe_customer=stripe_customer,
        status="active",
        plan=plan,
        seat_limit=1,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


@pytest.fixture
def team_subscription(stripe_customer, team_plan, team_plan_price):
    return Subscription.objects.create(
        stripe_id="sub_team_test_123",
        stripe_customer=stripe_customer,
        status="active",
        plan=team_plan,
        seat_limit=2,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


@pytest.fixture
def org_member_user(db):
    return User.objects.create_user(
        email="orgowner@example.com",
        full_name="Org Owner",
    )


@pytest.fixture
def org_member_client(org_member_user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=org_member_user)
    return client


@pytest.fixture
def org_member_stripe_customer(org_member_user):
    return StripeCustomer.objects.create(
        stripe_id="cus_org_test",
        user=org_member_user,
        livemode=False,
    )


@pytest.fixture
def authed_client(user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    return client


# Relax throttling in tests
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "billing": "1000/hour",
        "account": "1000/hour",
        "account_export": "1000/hour",
        "orgs": "1000/hour",
    },
    "EXCEPTION_HANDLER": "middleware.exceptions.domain_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF
