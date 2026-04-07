"""Tests for billing API views."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from rest_framework.test import APIClient
from saasmint_core.domain.stripe_customer import StripeCustomer as DomainStripeCustomer

from apps.billing.models import Plan, PlanPrice, Product, ProductPrice


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

    def test_caches_response(self, authed_client, plan, plan_price):
        resp1 = authed_client.get("/api/v1/billing/plans/")
        # Deactivate plan — cached response should still return it
        plan.is_active = False
        plan.save()
        resp2 = authed_client.get("/api/v1/billing/plans/")
        assert resp1.data == resp2.data

    def test_unauthenticated_allowed(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200


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
        self, mock_get_customer, mock_create, authed_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team", amount=2999
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
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
        assert resp.status_code == 204
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

    def test_caches_response(self, authed_client, product, product_price):
        resp1 = authed_client.get("/api/v1/billing/products/")
        product.is_active = False
        product.save()
        resp2 = authed_client.get("/api/v1/billing/products/")
        assert resp1.data == resp2.data

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
            },
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_team_plan_with_min_seats_succeeds(
        self, mock_get_customer, mock_create, authed_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Min", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_min", amount=2000
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
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
