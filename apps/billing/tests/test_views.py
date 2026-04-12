"""Tests for billing API views."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from rest_framework.test import APIClient
from saasmint_core.domain.stripe_customer import StripeCustomer as DomainStripeCustomer

from apps.billing.models import ExchangeRate, Plan, PlanPrice, Product, ProductPrice


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
        assert resp.data[0]["price"]["amount"] == 999

    def test_excludes_inactive_plans(self, authed_client, plan, plan_price):
        plan.is_active = False
        plan.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 0

    def test_response_includes_display_amount_and_currency(self, authed_client, plan, plan_price):
        resp = authed_client.get("/api/v1/billing/plans/")
        price = resp.data[0]["price"]
        assert price["currency"] == "usd"
        assert price["display_amount"] == 9.99

    def test_unauthenticated_allowed(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200

    def test_unauthenticated_returns_all_plans(self, plan, plan_price, team_plan, team_plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 2

    def test_personal_user_sees_only_personal_plans(
        self, authed_client, plan, plan_price, team_plan, team_plan_price
    ):
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]["context"] == "personal"

    def test_org_member_sees_only_team_plans(
        self, org_member_client, plan, plan_price, team_plan, team_plan_price
    ):
        resp = org_member_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data) == 1
        assert resp.data[0]["context"] == "team"


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
                "plan_price_id": str(plan_price.id),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert resp.data["url"] == "https://checkout.stripe.com/session"
        # The view must resolve the UUID to the underlying Stripe price ID
        # before calling Stripe.
        assert mock_create.call_args.kwargs["price_id"] == plan_price.stripe_price_id

    def test_invalid_plan_price_returns_404(self, authed_client):
        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(uuid4()),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 404

    def test_malformed_plan_price_id_returns_400(self, authed_client):
        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": "not-a-uuid",
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 400

    def test_missing_fields_returns_400(self, authed_client):
        resp = authed_client.post("/api/v1/billing/checkout-sessions/", {}, format="json")
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_trial_suppressed_for_team_plans(
        self, mock_get_customer, mock_create, org_member_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team", amount=2999
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "quantity": 2,
                "trial_period_days": 14,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Team Org",
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
                "plan_price_id": str(plan_price.id),
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
                "plan_price_id": str(plan_price.id),
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

    def test_returns_free_subscription(self, authed_client, free_subscription, free_plan):
        resp = authed_client.get("/api/v1/billing/subscription/")
        assert resp.status_code == 200
        assert resp.data["status"] == "active"
        assert str(resp.data["plan"]["id"]) == str(free_plan.id)

    def test_no_subscription_returns_404(self, authed_client, user):
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

    def test_free_subscription_returns_404(self, authed_client, free_subscription):
        """Cannot cancel a free-plan subscription via the API."""
        resp = authed_client.delete("/api/v1/billing/subscription/")
        assert resp.status_code == 404

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
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 204
        mock_change.assert_called_once()
        # The view must resolve the UUID to the underlying Stripe price ID.
        assert mock_change.call_args.kwargs["new_stripe_price_id"] == plan_price.stripe_price_id

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_plan_only_does_not_call_update_seat_count(
        self, mock_change, authed_client, subscription, plan_price
    ):
        with patch("apps.billing.views.update_seat_count", new_callable=AsyncMock) as mock_seats:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"plan_price_id": str(plan_price.id)},
                format="json",
            )
            mock_seats.assert_not_called()
        mock_change.assert_called_once()

    def test_free_subscription_returns_404(self, authed_client, free_subscription, plan_price):
        """Cannot change plan on a free subscription — must go through checkout."""
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 404

    def test_invalid_plan_returns_404(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": str(uuid4())},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_updates_seats(self, mock_seats, authed_client, team_subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 204
        mock_seats.assert_called_once()
        assert mock_seats.call_args.kwargs["quantity"] == 5

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_does_not_call_change_plan(
        self, mock_seats, authed_client, team_subscription
    ):
        with patch("apps.billing.views.change_plan", new_callable=AsyncMock) as mock_change:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"quantity": 3},
                format="json",
            )
            mock_change.assert_not_called()
        mock_seats.assert_called_once()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_rejected_on_personal_plan(self, mock_seats, authed_client, subscription):
        """Personal plans must not accept multi-seat updates via the seat-only path."""
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 400
        mock_seats.assert_not_called()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_rejected_below_team_minimum(
        self, mock_seats, authed_client, team_subscription
    ):
        """Team plans must reject seat updates below the minimum seat count."""
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"quantity": 1},
            format="json",
        )
        assert resp.status_code == 400
        mock_seats.assert_not_called()

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
            {"plan_price_id": str(team_plan_price.id), "quantity": 3},
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
                {"plan_price_id": str(team_plan_price.id), "quantity": 3},
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
            {"plan_price_id": str(plan_price.id), "prorate": False},
            format="json",
        )
        assert mock_change.call_args.kwargs["prorate"] is False

    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_true_calls_cancel(self, mock_cancel, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 204
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_false_calls_resume(
        self, mock_resume, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 204
        mock_resume.assert_called_once()

    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_toggle_does_not_call_change_plan(
        self, mock_cancel, mock_resume, authed_client, subscription
    ):
        with patch("apps.billing.views.change_plan", new_callable=AsyncMock) as mock_change:
            authed_client.patch(
                "/api/v1/billing/subscription/",
                {"cancel_at_period_end": True},
                format="json",
            )
            mock_change.assert_not_called()

    def test_cancel_at_period_end_with_plan_returns_400(
        self, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": str(plan_price.id), "cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 400

    def test_cancel_at_period_end_free_subscription_returns_404(
        self, authed_client, free_subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/billing/subscription/", {"quantity": 5}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestProductListView:
    @pytest.fixture
    def product(self):
        return Product.objects.create(
            name="100 Credits",
            type="one_time",
            credits=100,
            is_active=True,
        )

    @pytest.fixture
    def product_price(self, product):
        return ProductPrice.objects.create(
            product=product,
            stripe_price_id="price_credits_100",
            amount=999,
        )

    def test_returns_active_products(self, authed_client, product, product_price):
        resp = authed_client.get("/api/v1/billing/products/")
        assert resp.status_code == 200
        match = next(p for p in resp.data if p["name"] == "100 Credits")
        assert match["credits"] == 100
        assert match["type"] == "one_time"
        assert match["price"]["amount"] == 999

    def test_excludes_inactive_products(self, authed_client, product, product_price):
        product.is_active = False
        product.save()
        resp = authed_client.get("/api/v1/billing/products/")
        assert resp.status_code == 200
        assert not any(p["name"] == "100 Credits" for p in resp.data)

    def test_response_includes_display_amount_and_currency(
        self, authed_client, product, product_price
    ):
        resp = authed_client.get("/api/v1/billing/products/")
        price = resp.data[0]["price"]
        assert price["currency"] == "usd"
        assert price["display_amount"] == 9.99

    def test_unauthenticated_rejected(self, product, product_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/products/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestQuantityValidationOnCheckout:
    """Tests for _validate_quantity_for_plan via the checkout endpoint."""

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_personal_plan_with_quantity_gt_1_returns_400(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(plan_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_team_plan_with_quantity_lt_2_returns_400(
        self, mock_get_customer, mock_create, authed_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Mini", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_mini", amount=1500
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "quantity": 1,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Mini Org",
            },
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_team_plan_with_min_seats_succeeds(
        self, mock_get_customer, mock_create, org_member_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Min", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_min", amount=2000
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Min Org",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert mock_create.call_args.kwargs["quantity"] == 2


@pytest.mark.django_db
class TestUpdateSubscriptionQuantityValidation:
    """Quantity-rule validation through PATCH /subscription/."""

    def test_personal_plan_with_quantity_gt_1_returns_400(
        self, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": str(plan_price.id), "quantity": 2},
            format="json",
        )
        assert resp.status_code == 400

    def test_team_plan_with_quantity_lt_2_returns_400(
        self, authed_client, subscription, team_plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscription/",
            {"plan_price_id": str(team_plan_price.id), "quantity": 1},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestCurrencyConversion:
    """Display-currency conversion on plan/product/subscription endpoints."""

    def test_currency_query_param_converts_amount(self, plan, plan_price):
        ExchangeRate.objects.create(
            currency="eur",
            rate="0.91",
            fetched_at=datetime.now(UTC),
        )
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=eur")
        price = resp.data[0]["price"]
        assert price["currency"] == "eur"
        # 999 cents * 0.91 = 909.09 → round → 909 minor units → 9.09 → friendly → 9.49
        assert price["display_amount"] == 9.49
        assert price["approximate"] is True
        # Original USD cents still present
        assert price["amount"] == 999

    def test_falls_back_to_usd_when_rate_missing(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=eur")
        price = resp.data[0]["price"]
        # No ExchangeRate for EUR → fallback to USD
        assert price["currency"] == "usd"
        assert price["display_amount"] == 9.99
        assert price["approximate"] is False

    def test_invalid_currency_param_ignored(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=xyz")
        assert resp.data[0]["price"]["currency"] == "usd"

    def test_authenticated_user_preferred_currency(self, authed_client, user, plan, plan_price):
        ExchangeRate.objects.create(
            currency="gbp",
            rate="0.79",
            fetched_at=datetime.now(UTC),
        )
        user.preferred_currency = "gbp"
        user.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data[0]["price"]["currency"] == "gbp"

    def test_query_param_overrides_user_preference(self, authed_client, user, plan, plan_price):
        ExchangeRate.objects.create(
            currency="eur",
            rate="0.91",
            fetched_at=datetime.now(UTC),
        )
        ExchangeRate.objects.create(
            currency="gbp",
            rate="0.79",
            fetched_at=datetime.now(UTC),
        )
        user.preferred_currency = "gbp"
        user.save()
        resp = authed_client.get("/api/v1/billing/plans/?currency=eur")
        assert resp.data[0]["price"]["currency"] == "eur"

    def test_zero_decimal_currency_conversion(self, plan, plan_price):
        ExchangeRate.objects.create(
            currency="jpy",
            rate="149.5",
            fetched_at=datetime.now(UTC),
        )
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=jpy")
        price = resp.data[0]["price"]
        assert price["currency"] == "jpy"
        # 999 * 149.5 = 149350.5 → round → 149350 → zero-decimal → friendly → 149400.0
        assert price["display_amount"] == 149400.0
        assert price["approximate"] is True

    def test_subscription_includes_currency(self, authed_client, subscription):
        resp = authed_client.get("/api/v1/billing/subscription/")
        price = resp.data["plan"]["price"]
        assert "currency" in price
        assert "display_amount" in price

    def test_product_endpoint_currency_conversion(self, authed_client):
        """Products endpoint also respects ?currency= param."""
        product = Product.objects.create(
            name="50 Credits", type="one_time", credits=50, is_active=True
        )
        ProductPrice.objects.create(product=product, stripe_price_id="price_prod_cur", amount=500)
        ExchangeRate.objects.create(currency="eur", rate="0.91", fetched_at=datetime.now(UTC))
        resp = authed_client.get("/api/v1/billing/products/?currency=eur")
        price = resp.data[0]["price"]
        assert price["currency"] == "eur"
        # 500 * 0.91 = 455 → /100 → 4.55 → friendly → 4.99 (rounds up)
        assert price["display_amount"] == 4.99
        assert price["approximate"] is True

    def test_user_default_currency_returns_usd(self, authed_client, user, plan, plan_price):
        """User with default preferred_currency='usd' gets USD without exchange rate lookup."""
        assert user.preferred_currency == "usd"
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data[0]["price"]["currency"] == "usd"

    def test_user_unsupported_preferred_currency_falls_back_to_usd(
        self, authed_client, user, plan, plan_price
    ):
        """User with a preferred_currency not in SUPPORTED_CURRENCIES should get USD."""
        user.preferred_currency = "xyz"
        user.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data[0]["price"]["currency"] == "usd"

    def test_empty_currency_param_ignored(self, plan, plan_price):
        """?currency= (empty string) should fall back to USD."""
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=")
        assert resp.data[0]["price"]["currency"] == "usd"
