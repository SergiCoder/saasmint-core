"""Tests for billing API views."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from rest_framework.test import APIClient
from saasmint_core.domain.stripe_customer import StripeCustomer as DomainStripeCustomer

from apps.billing.models import Plan, PlanPrice


@pytest.fixture
def mock_stripe_customer():
    return DomainStripeCustomer(
        id=uuid4(),
        stripe_id="cus_test",
        user_id=uuid4(),
        org_id=None,
        livemode=False,
        created_at=datetime.now(UTC),
    )


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
class TestCheckoutSessionView:
    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_creates_session(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "price_test_123",
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["url"] == "https://checkout.stripe.com/session"

    def test_invalid_plan_price_returns_404(self, authed_client):
        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "price_nonexistent",
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_fields_returns_400(self, authed_client):
        resp = authed_client.post("/api/v1/billing/checkout-sessions/", {}, format="json")
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_trial_suppressed_for_team_plans(
        self, mock_get_customer, mock_create, authed_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team", currency="usd", amount=2999
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "price_team",
                "quantity": 2,
                "trial_period_days": 14,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        # trial_period_days should be None for team plans
        assert mock_create.call_args.kwargs["trial_period_days"] is None

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_trial_preserved_for_personal_plans(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "price_test_123",
                "trial_period_days": 7,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        # trial_period_days should be preserved for personal plans
        assert mock_create.call_args.kwargs["trial_period_days"] == 7

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_checkout_response_includes_location_header(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "price_test_123",
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp["Location"] == "https://checkout.stripe.com/session"

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post("/api/v1/billing/checkout-sessions/", {}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestPortalSessionView:
    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_creates_portal_session(
        self, mock_get_customer, mock_portal, authed_client, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_portal.return_value = "https://billing.stripe.com/portal"

        resp = authed_client.post(
            "/api/v1/billing/portal-sessions/",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["url"] == "https://billing.stripe.com/portal"

    def test_invalid_return_url_rejected(self, authed_client, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://example.com"]
        settings.ALLOWED_HOSTS = ["example.com"]
        resp = authed_client.post(
            "/api/v1/billing/portal-sessions/",
            {"return_url": "https://evil.com/portal"},
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_portal_response_includes_location_header(
        self, mock_get_customer, mock_portal, authed_client, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_portal.return_value = "https://billing.stripe.com/portal"

        resp = authed_client.post(
            "/api/v1/billing/portal-sessions/",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp["Location"] == "https://billing.stripe.com/portal"

    def test_missing_body_returns_400(self, authed_client):
        resp = authed_client.post("/api/v1/billing/portal-sessions/", {}, format="json")
        assert resp.status_code == 400

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post("/api/v1/billing/portal-sessions/", {}, format="json")
        assert resp.status_code in (401, 403)


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

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/billing/subscription/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestCancelSubscription:
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancels_subscription(self, mock_cancel, authed_client, subscription):
        resp = authed_client.delete("/api/v1/billing/subscription/")
        assert resp.status_code == 204
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.delete("/api/v1/billing/subscription/")
        assert resp.status_code == 404

    def test_no_active_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.delete("/api/v1/billing/subscription/")
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.delete("/api/v1/billing/subscription/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestUpdateSubscription:
    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_changes_plan(self, mock_change, authed_client, subscription, plan_price):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": "price_test_123"},
            format="json",
        )
        assert resp.status_code == 204
        mock_change.assert_called_once()

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_plan_only_does_not_call_update_seat_count(
        self, mock_change, authed_client, subscription, plan_price
    ):
        with patch("apps.billing.views.update_seat_count", new_callable=AsyncMock) as mock_seats:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"plan_price_id": "price_test_123"},
                format="json",
            )
            mock_seats.assert_not_called()
        mock_change.assert_called_once()

    def test_invalid_plan_returns_404(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": "price_nonexistent"},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_updates_seats(self, mock_seats, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 204
        mock_seats.assert_called_once()
        assert mock_seats.call_args.kwargs["quantity"] == 5

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_does_not_call_change_plan(self, mock_seats, authed_client, subscription):
        with patch("apps.billing.views.change_plan", new_callable=AsyncMock) as mock_change:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"quantity": 3},
                format="json",
            )
            mock_change.assert_not_called()
        mock_seats.assert_called_once()

    def test_invalid_quantity_returns_400(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 0},
            format="json",
        )
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, authed_client, subscription):
        resp = authed_client.patch("/api/v1/billing/subscription/", {}, format="json")
        assert resp.status_code == 400

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 404

    def test_customer_without_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_combined_plan_and_seats_update(
        self, mock_change, authed_client, subscription, team_plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": "price_team_123", "quantity": 3},
            format="json",
        )
        assert resp.status_code == 204
        mock_change.assert_called_once()
        assert mock_change.call_args.kwargs["quantity"] == 3

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_combined_update_does_not_call_update_seat_count(
        self, mock_change, authed_client, subscription, team_plan_price
    ):
        """When both plan_price_id and quantity are sent, only change_plan is called
        (with quantity kwarg) — update_seat_count must NOT be called separately."""
        with patch("apps.billing.views.update_seat_count", new_callable=AsyncMock) as mock_seats:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"plan_price_id": "price_team_123", "quantity": 3},
                format="json",
            )
            mock_seats.assert_not_called()
        mock_change.assert_called_once()

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_prorate_kwarg_passed_to_change_plan(
        self, mock_change, authed_client, subscription, plan_price
    ):
        authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": "price_test_123", "prorate": False},
            format="json",
        )
        assert mock_change.call_args.kwargs["prorate"] is False

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/billing/subscription/", {"quantity": 5}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestApplyPromoCodeView:
    @patch("apps.billing.views.apply_promo_code", new_callable=AsyncMock)
    def test_applies_promo(self, mock_promo, authed_client, subscription):
        resp = authed_client.post(
            "/api/v1/billing/subscription/promo-code/",
            {"promo_code": "SAVE20"},
            format="json",
        )
        assert resp.status_code == 200
        mock_promo.assert_called_once()

    def test_no_subscription_returns_404(self, authed_client, user):
        resp = authed_client.post(
            "/api/v1/billing/subscription/promo-code/",
            {"promo_code": "SAVE20"},
            format="json",
        )
        assert resp.status_code == 404

    def test_missing_promo_code_returns_400(self, authed_client, subscription):
        resp = authed_client.post("/api/v1/billing/subscription/promo-code/", {}, format="json")
        assert resp.status_code == 400

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post(
            "/api/v1/billing/subscription/promo-code/",
            {"promo_code": "SAVE20"},
            format="json",
        )
        assert resp.status_code in (401, 403)
