"""Shared fixtures for the dashboard test package."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from django.test import Client

from apps.billing.models import Plan, StripeCustomer, Subscription
from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import User


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="dashboard@example.com",
        supabase_uid="sup_dashboard",
        full_name="Dashboard User",
    )


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="staff@example.com",
        supabase_uid="sup_staff",
        full_name="Staff User",
        is_staff=True,
        is_superuser=True,
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
def stripe_customer(user):
    return StripeCustomer.objects.create(
        stripe_id="cus_dash_test",
        user=user,
        livemode=False,
    )


@pytest.fixture
def active_subscription(stripe_customer, plan):
    return Subscription.objects.create(
        stripe_id="sub_dash_test",
        stripe_customer=stripe_customer,
        status="active",
        plan=plan,
        quantity=1,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


@pytest.fixture
def org(user):
    return Org.objects.create(
        name="Test Org",
        slug="test-org",
        created_by=user,
    )


@pytest.fixture
def org_membership(org, user):
    return OrgMember.objects.create(
        org=org,
        user=user,
        role=OrgRole.OWNER,
    )


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def staff_client(staff_user):
    client = Client()
    client.force_login(staff_user)
    return client
