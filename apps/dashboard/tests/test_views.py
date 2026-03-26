"""Tests for DashboardView and hijack URL overrides."""

from __future__ import annotations

import pytest
from django.test import Client

from apps.users.models import User


@pytest.mark.django_db
class TestDashboardViewAuth:
    def test_unauthenticated_redirects_to_login(self):
        client = Client()
        resp = client.get("/dashboard/")
        assert resp.status_code == 302
        assert "/admin/login/" in resp["Location"]

    def test_authenticated_user_gets_200(self, logged_in_client):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200


@pytest.mark.django_db
class TestDashboardViewContext:
    def test_subscription_none_when_no_stripe_customer(self, logged_in_client):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert resp.context["subscription"] is None

    def test_subscription_none_when_customer_exists_but_no_active_sub(
        self, logged_in_client, stripe_customer
    ):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert resp.context["subscription"] is None

    def test_subscription_returned_when_active(self, logged_in_client, active_subscription):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        sub = resp.context["subscription"]
        assert sub is not None
        assert sub.status == "active"
        assert sub.stripe_id == "sub_dash_test"

    def test_org_memberships_empty_when_no_orgs(self, logged_in_client):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert list(resp.context["org_memberships"]) == []

    def test_org_memberships_returned_when_member(self, logged_in_client, org_membership):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        memberships = list(resp.context["org_memberships"])
        assert len(memberships) == 1
        assert memberships[0].org.slug == "test-org"

    def test_subscription_inactive_status_not_returned(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_canceled_test",
            stripe_customer=stripe_customer,
            status="canceled",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert resp.context["subscription"] is None

    def test_most_recent_active_subscription_returned(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        sub1 = Subscription.objects.create(
            stripe_id="sub_older",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2025, 11, 1, tzinfo=UTC),
            current_period_end=datetime(2025, 12, 1, tzinfo=UTC),
        )
        sub2 = Subscription.objects.create(
            stripe_id="sub_newer",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            quantity=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        # The most recently created subscription should be returned
        returned = resp.context["subscription"]
        assert returned is not None
        assert returned.stripe_id == sub2.stripe_id
        assert sub1.stripe_id != returned.stripe_id


@pytest.mark.django_db
class TestDashboardTemplate:
    def test_renders_user_email(self, logged_in_client, user):
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert user.email.encode() in resp.content

    def test_no_subscription_shows_fallback_text(self, logged_in_client):
        resp = logged_in_client.get("/dashboard/")
        assert b"No active subscription" in resp.content

    def test_active_subscription_shows_plan_name(self, logged_in_client, active_subscription):
        resp = logged_in_client.get("/dashboard/")
        assert b"Personal Monthly" in resp.content


@pytest.mark.django_db
class TestDashboardTemplateSubscriptionBadges:
    def test_trialing_subscription_shows_trialing_badge(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_trialing_badge",
            stripe_customer=stripe_customer,
            status="trialing",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"Trialing" in resp.content

    def test_past_due_subscription_shows_past_due_badge(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_past_due_badge",
            stripe_customer=stripe_customer,
            status="past_due",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"Past due" in resp.content

    def test_subscription_with_trial_end_date_shows_trial_section(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_with_trial",
            stripe_customer=stripe_customer,
            status="trialing",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            trial_ends_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"Trial ends" in resp.content

    def test_subscription_with_discount_shows_discount_section(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_with_discount",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            discount_percent=20,
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"Discount" in resp.content
        assert b"20%" in resp.content

    def test_subscription_with_discount_and_end_date_shows_until_date(
        self, logged_in_client, stripe_customer, plan
    ):
        from datetime import UTC, datetime

        from apps.billing.models import Subscription

        Subscription.objects.create(
            stripe_id="sub_with_discount_end",
            stripe_customer=stripe_customer,
            status="active",
            plan=plan,
            quantity=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
            discount_percent=15,
            discount_end_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
        resp = logged_in_client.get("/dashboard/")
        assert resp.status_code == 200
        assert b"until" in resp.content


@pytest.mark.django_db
class TestHijackAcquireView:
    def test_acquire_redirects_to_dashboard_on_success(self, staff_client, user):
        resp = staff_client.post(
            "/hijack/acquire/",
            {"user_pk": str(user.pk)},
        )
        # hijack returns a redirect on success
        assert resp.status_code == 302
        assert "/dashboard/" in resp["Location"]

    def test_acquire_requires_staff(self, logged_in_client, user):
        other = User.objects.create_user(
            email="target@example.com",
            supabase_uid="sup_target",
        )
        resp = logged_in_client.post(
            "/hijack/acquire/",
            {"user_pk": str(other.pk)},
        )
        assert resp.status_code in (302, 403)


@pytest.mark.django_db
class TestHijackReleaseView:
    def test_release_redirects_to_admin_user_changelist(self, staff_client, user):
        # First acquire the user so there is a session to release
        staff_client.post("/hijack/acquire/", {"user_pk": str(user.pk)})
        resp = staff_client.post("/hijack/release/")
        assert resp.status_code == 302
        assert "/admin/" in resp["Location"]
