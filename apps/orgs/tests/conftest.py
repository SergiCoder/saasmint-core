"""Shared fixtures for the orgs test package."""

from __future__ import annotations

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

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
        email="orgowner@example.com",
        supabase_uid="sup_orgowner",
        full_name="Org Owner",
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        email="other@example.com",
        supabase_uid="sup_other",
        full_name="Other User",
    )


@pytest.fixture
def org(user):
    return Org.objects.create(
        name="Test Org",
        slug="test-org",
        created_by=user,
    )


@pytest.fixture
def owner_membership(org, user):
    return OrgMember.objects.create(
        org=org,
        user=user,
        role=OrgRole.OWNER,
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        email="admin@example.com",
        supabase_uid="sup_admin",
        full_name="Admin User",
    )


@pytest.fixture
def admin_membership(org, admin_user):
    return OrgMember.objects.create(
        org=org,
        user=admin_user,
        role=OrgRole.ADMIN,
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        email="member@example.com",
        supabase_uid="sup_member",
        full_name="Member User",
    )


@pytest.fixture
def member_membership(org, member_user):
    return OrgMember.objects.create(
        org=org,
        user=member_user,
        role=OrgRole.MEMBER,
    )


@pytest.fixture
def authed_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def admin_client(admin_user):
    client = APIClient()
    client.force_authenticate(user=admin_user)
    return client


@pytest.fixture
def member_client(member_user):
    client = APIClient()
    client.force_authenticate(user=member_user)
    return client


@pytest.fixture
def soft_deleted_org(org):
    from django.utils import timezone

    org.deleted_at = timezone.now()
    org.save(update_fields=["deleted_at"])
    return org


@pytest.fixture
def second_admin_user(db):
    return User.objects.create_user(
        email="admin2@example.com",
        supabase_uid="sup_admin2",
        full_name="Admin2",
    )


@pytest.fixture
def second_admin_membership(org, second_admin_user):
    return OrgMember.objects.create(
        org=org,
        user=second_admin_user,
        role=OrgRole.ADMIN,
    )


@pytest.fixture
def second_admin_client(second_admin_user):
    client = APIClient()
    client.force_authenticate(user=second_admin_user)
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
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF
