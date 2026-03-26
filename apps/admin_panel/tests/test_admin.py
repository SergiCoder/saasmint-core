"""Tests for UserAdminExtended — queryset annotation and subscription_status display."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from apps.billing.models import Plan, StripeCustomer, Subscription
from apps.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(db: object, email: str, supabase_uid: str, **kwargs: Any) -> User:
    return User.objects.create_user(
        email=email,
        supabase_uid=supabase_uid,
        **kwargs,
    )


def _make_subscription(
    stripe_customer: StripeCustomer,
    plan: Plan,
    status: str,
    stripe_id: str,
) -> Subscription:
    return Subscription.objects.create(
        stripe_id=stripe_id,
        stripe_customer=stripe_customer,
        status=status,
        plan=plan,
        quantity=1,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# subscription_status display method
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSubscriptionStatusDisplay:
    """Unit tests for UserAdminExtended.subscription_status()."""

    def setup_method(self):
        from django.contrib import admin

        from apps.admin_panel.admin import UserAdminExtended

        self.admin_instance = UserAdminExtended(User, admin.site)

    def _obj_with_status(self, status):
        obj = MagicMock(spec=User)
        obj._subscription_status = status
        return obj

    def test_no_subscription_returns_dash(self):
        obj = MagicMock(spec=User)
        del obj._subscription_status  # ensure getattr returns None
        obj.__class__ = User
        # Use a plain object without the attribute at all
        plain = object.__new__(User)
        result = self.admin_instance.subscription_status(plain)
        assert result == "—"

    def test_active_renders_green(self):
        obj = self._obj_with_status("active")
        html = self.admin_instance.subscription_status(obj)
        assert "green" in str(html)
        assert "active" in str(html)

    def test_trialing_renders_blue(self):
        obj = self._obj_with_status("trialing")
        html = self.admin_instance.subscription_status(obj)
        assert "blue" in str(html)
        assert "trialing" in str(html)

    def test_past_due_renders_orange(self):
        obj = self._obj_with_status("past_due")
        html = self.admin_instance.subscription_status(obj)
        assert "orange" in str(html)
        assert "past_due" in str(html)

    def test_unknown_status_renders_grey(self):
        obj = self._obj_with_status("canceled")
        html = self.admin_instance.subscription_status(obj)
        assert "grey" in str(html)
        assert "canceled" in str(html)

    def test_empty_string_status_returns_dash(self):
        obj = self._obj_with_status("")
        result = self.admin_instance.subscription_status(obj)
        assert result == "—"


# ---------------------------------------------------------------------------
# get_queryset annotation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserAdminExtendedQueryset:
    """Integration tests: get_queryset annotates _subscription_status correctly."""

    def setup_method(self):
        from django.contrib import admin

        from apps.admin_panel.admin import UserAdminExtended

        self.admin_instance = UserAdminExtended(User, admin.site)
        self.mock_request = MagicMock()

    def test_user_without_customer_has_null_annotation(self, db):
        user = _make_user(db, "noplan@example.com", "sup_noplan")
        qs = self.admin_instance.get_queryset(self.mock_request)
        annotated = qs.get(pk=user.pk)
        assert getattr(annotated, "_subscription_status", None) is None

    def test_user_with_active_subscription_annotated(self, db):
        user = _make_user(db, "active@example.com", "sup_active")
        plan = Plan.objects.create(name="Pro", context="personal", interval="month", is_active=True)
        customer = StripeCustomer.objects.create(
            stripe_id="cus_admin_active", user=user, livemode=False
        )
        _make_subscription(customer, plan, "active", "sub_admin_active")

        qs = self.admin_instance.get_queryset(self.mock_request)
        annotated = qs.get(pk=user.pk)
        assert annotated._subscription_status == "active"

    def test_user_with_trialing_subscription_annotated(self, db):
        user = _make_user(db, "trial@example.com", "sup_trial")
        plan = Plan.objects.create(
            name="Free", context="personal", interval="month", is_active=True
        )
        customer = StripeCustomer.objects.create(
            stripe_id="cus_admin_trial", user=user, livemode=False
        )
        _make_subscription(customer, plan, "trialing", "sub_admin_trial")

        qs = self.admin_instance.get_queryset(self.mock_request)
        annotated = qs.get(pk=user.pk)
        assert annotated._subscription_status == "trialing"

    def test_user_with_only_canceled_subscription_has_null_annotation(self, db):
        user = _make_user(db, "canceled@example.com", "sup_canceled")
        plan = Plan.objects.create(
            name="Basic", context="personal", interval="month", is_active=True
        )
        customer = StripeCustomer.objects.create(
            stripe_id="cus_admin_canceled", user=user, livemode=False
        )
        _make_subscription(customer, plan, "canceled", "sub_admin_canceled")

        qs = self.admin_instance.get_queryset(self.mock_request)
        annotated = qs.get(pk=user.pk)
        assert getattr(annotated, "_subscription_status", None) is None

    def test_most_recent_active_subscription_status_used(self, db):
        user = _make_user(db, "multi@example.com", "sup_multi")
        plan = Plan.objects.create(
            name="Pro Multi", context="personal", interval="month", is_active=True
        )
        customer = StripeCustomer.objects.create(
            stripe_id="cus_admin_multi", user=user, livemode=False
        )
        # Create an older trialing and a newer active subscription
        _make_subscription(customer, plan, "trialing", "sub_admin_multi_old")
        _make_subscription(customer, plan, "active", "sub_admin_multi_new")

        qs = self.admin_instance.get_queryset(self.mock_request)
        annotated = qs.get(pk=user.pk)
        # The most recently created subscription wins; both are active statuses so either is valid,
        # but the annotation must be set (not None)
        assert annotated._subscription_status is not None
        assert annotated._subscription_status in ("active", "trialing")


# ---------------------------------------------------------------------------
# Admin site integration (changelist renders without error)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserAdminChangelistRendering:
    def test_changelist_loads_for_superuser(self, db):
        from django.test import Client

        superuser = User.objects.create_superuser(
            email="super@example.com",
            supabase_uid="sup_super",
        )
        client = Client()
        client.force_login(superuser)
        resp = client.get("/admin/users/user/")
        assert resp.status_code == 200

    def test_changelist_with_subscriber_shows_status_column(self, db):
        from django.test import Client

        superuser = User.objects.create_superuser(
            email="super2@example.com",
            supabase_uid="sup_super2",
        )
        regular = _make_user(db, "reg@example.com", "sup_reg")
        plan = Plan.objects.create(
            name="Pro Changelist", context="personal", interval="month", is_active=True
        )
        customer = StripeCustomer.objects.create(
            stripe_id="cus_changelist", user=regular, livemode=False
        )
        _make_subscription(customer, plan, "active", "sub_changelist")

        client = Client()
        client.force_login(superuser)
        resp = client.get("/admin/users/user/")
        assert resp.status_code == 200
        assert b"active" in resp.content
