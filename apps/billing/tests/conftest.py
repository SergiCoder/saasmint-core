"""Shared fixtures for the billing test package."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.core.cache import cache

from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription
from apps.users.models import User


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="billing@example.com",
        supabase_uid="sup_billing",
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
        currency="usd",
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
        currency="usd",
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
def subscription(stripe_customer, plan):
    return Subscription.objects.create(
        stripe_id="sub_test_123",
        stripe_customer=stripe_customer,
        status="active",
        plan=plan,
        quantity=1,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


@pytest.fixture
def free_plan(db):
    plan = Plan.objects.create(
        name="Personal Free",
        context="personal",
        interval="month",
        is_active=True,
    )
    PlanPrice.objects.create(plan=plan, stripe_price_id="price_free_usd", currency="usd", amount=0)
    return plan


@pytest.fixture
def free_subscription(free_plan, user):
    return Subscription.objects.create(
        user=user,
        status="active",
        plan=free_plan,
        quantity=1,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
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
