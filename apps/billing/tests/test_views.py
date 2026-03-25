"""Tests for billing API views."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from rest_framework.test import APIClient

from apps.billing.models import Plan, PlanPrice


@pytest.mark.django_db
class TestPlanListView:
    def test_returns_active_plans(self, authed_client, plan, plan_price):
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]["name"] == "Personal Monthly"
        assert len(resp.data[0]["prices"]) == 1

    def test_excludes_inactive_plans(self, authed_client, plan, plan_price):
        plan.is_active = False
        plan.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 0

    def test_caches_response(self, authed_client, plan, plan_price):
        resp1 = authed_client.get("/api/v1/billing/plans/")
        # Deactivate plan — cached response should still return it
        plan.is_active = False
        plan.save()
        resp2 = authed_client.get("/api/v1/billing/plans/")
        assert resp1.data == resp2.data

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestCheckoutView:
    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_creates_session(self, mock_get_customer, mock_create, authed_client, plan_price):
        from uuid import uuid4

        from stripe_saas_core.domain.stripe_customer import StripeCustomer

        mock_get_customer.return_value = StripeCustomer(
            id=uuid4(),
            stripe_id="cus_test",
            user_id=uuid4(),
            org_id=None,
            livemode=False,
            created_at=datetime.now(UTC),
        )
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout/",
            {
                "plan_price_id": "price_test_123",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["url"] == "https://checkout.stripe.com/session"

    def test_invalid_plan_price_returns_404(self, authed_client):
        resp = authed_client.post(
            "/api/v1/billing/checkout/",
            {
                "plan_price_id": "price_nonexistent",
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_fields_returns_400(self, authed_client):
        resp = authed_client.post("/api/v1/billing/checkout/", {}, format="json")
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_trial_suppressed_for_team_plans(
        self, mock_get_customer, mock_create, authed_client, db
    ):
        from stripe_saas_core.domain.stripe_customer import StripeCustomer

        team_plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team", currency="usd", amount=2999
        )
        from uuid import uuid4

        mock_get_customer.return_value = StripeCustomer(
            id=uuid4(),
            stripe_id="cus_test",
            user_id=uuid4(),
            org_id=None,
            livemode=False,
            created_at=datetime.now(UTC),
        )
        mock_create.return_value = "https://checkout.stripe.com/session"

        authed_client.post(
            "/api/v1/billing/checkout/",
            {
                "plan_price_id": "price_team",
                "trial_period_days": 14,
                "success_url": "https://example.com/success",
                "cancel_url": "https://example.com/cancel",
            },
            format="json",
        )
        # trial_period_days should be None for team plans
        assert mock_create.call_args.kwargs["trial_period_days"] is None

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post("/api/v1/billing/checkout/", {}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestPortalView:
    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_creates_portal_session(self, mock_get_customer, mock_portal, authed_client):
        from uuid import uuid4

        from stripe_saas_core.domain.stripe_customer import StripeCustomer

        mock_get_customer.return_value = StripeCustomer(
            id=uuid4(),
            stripe_id="cus_test",
            user_id=uuid4(),
            org_id=None,
            livemode=False,
            created_at=datetime.now(UTC),
        )
        mock_portal.return_value = "https://billing.stripe.com/portal"

        resp = authed_client.post(
            "/api/v1/billing/portal/",
            {"return_url": "https://example.com/dashboard"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["url"] == "https://billing.stripe.com/portal"

    def test_invalid_return_url_rejected(self, authed_client, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        settings.ALLOWED_HOSTS = ["example.com"]
        resp = authed_client.post(
            "/api/v1/billing/portal/",
            {"return_url": "https://evil.com/portal"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestSubscriptionView:
    def test_returns_active_subscription(self, authed_client, subscription):
        resp = authed_client.get("/api/v1/billing/subscription/")
        assert resp.status_code == 200
        assert resp.data["status"] == "active"

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.get("/api/v1/billing/subscription/")
        assert resp.status_code == 404

    def test_no_active_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.get("/api/v1/billing/subscription/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestCancelSubscriptionView:
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancels_subscription(self, mock_cancel, authed_client, subscription):
        resp = authed_client.post("/api/v1/billing/subscription/cancel/")
        assert resp.status_code == 200
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.post("/api/v1/billing/subscription/cancel/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestChangePlanView:
    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_changes_plan(self, mock_change, authed_client, subscription, plan_price):
        resp = authed_client.post(
            "/api/v1/billing/subscription/change-plan/",
            {"plan_price_id": "price_test_123"},
            format="json",
        )
        assert resp.status_code == 200
        mock_change.assert_called_once()

    def test_invalid_plan_returns_404(self, authed_client, subscription):
        resp = authed_client.post(
            "/api/v1/billing/subscription/change-plan/",
            {"plan_price_id": "price_nonexistent"},
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_fields_returns_400(self, authed_client, subscription):
        resp = authed_client.post("/api/v1/billing/subscription/change-plan/", {}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestApplyPromoCodeView:
    @patch("apps.billing.views.apply_promo_code", new_callable=AsyncMock)
    def test_applies_promo(self, mock_promo, authed_client, subscription):
        resp = authed_client.post(
            "/api/v1/billing/subscription/promo/",
            {"promo_code": "SAVE20"},
            format="json",
        )
        assert resp.status_code == 200
        mock_promo.assert_called_once()

    def test_no_subscription_returns_404(self, authed_client, user):
        resp = authed_client.post(
            "/api/v1/billing/subscription/promo/",
            {"promo_code": "SAVE20"},
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_promo_code_returns_400(self, authed_client, subscription):
        resp = authed_client.post("/api/v1/billing/subscription/promo/", {}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestUpdateSeatCountView:
    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_updates_seats(self, mock_seats, authed_client, subscription):
        resp = authed_client.post(
            "/api/v1/billing/subscription/seats/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 200
        mock_seats.assert_called_once()

    def test_invalid_quantity_returns_400(self, authed_client, subscription):
        resp = authed_client.post(
            "/api/v1/billing/subscription/seats/",
            {"quantity": 0},
            format="json",
        )
        assert resp.status_code == 400

    def test_no_subscription_returns_404(self, authed_client, user):
        resp = authed_client.post(
            "/api/v1/billing/subscription/seats/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 404
