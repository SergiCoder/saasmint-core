"""Tests for billing API views."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
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
        assert len(resp.data["results"]) == 1
        assert resp.data["results"][0]["name"] == "Personal Monthly"
        assert resp.data["results"][0]["price"]["amount"] == 999

    def test_excludes_inactive_plans(self, authed_client, plan, plan_price):
        plan.is_active = False
        plan.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 0

    def test_response_includes_display_amount_and_currency(self, authed_client, plan, plan_price):
        resp = authed_client.get("/api/v1/billing/plans/")
        price = resp.data["results"][0]["price"]
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
        assert len(resp.data["results"]) == 2

    def test_personal_user_sees_only_personal_plans(
        self, authed_client, plan, plan_price, team_plan, team_plan_price
    ):
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 1
        assert resp.data["results"][0]["context"] == "personal"

    def test_org_member_sees_only_team_plans(
        self, org_member_client, plan, plan_price, team_plan, team_plan_price
    ):
        resp = org_member_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 1
        assert resp.data["results"][0]["context"] == "team"


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
        assert resp.status_code == 200
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
    def test_checkout_response_has_no_location_header(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        """The Stripe URL is not a local resource, so Location must not be set."""
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
        assert "Location" not in resp

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.post("/api/v1/billing/checkout-sessions/", {}, format="json")
        assert resp.status_code in (401, 403)

    def test_personal_user_cannot_checkout_team_plan(
        self, authed_client, team_plan, team_plan_price
    ):
        """Personal account_type users are rejected when checking out a team plan."""
        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "My Org",
            },
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "account_type_mismatch"
        assert "org accounts" in resp.data["detail"].lower()

    def test_org_member_cannot_checkout_personal_plan(self, org_member_client, plan_price):
        """Org member account_type users are rejected when checking out a personal plan."""
        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(plan_price.id),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "account_type_mismatch"
        assert "personal plans" in resp.data["detail"].lower()

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_team_checkout_requires_org_name(
        self,
        mock_get_customer,
        mock_create,
        org_member_client,
        org_member_stripe_customer,
        team_plan,
        team_plan_price,
    ):
        """Team plan checkout must include org_name."""
        mock_get_customer.return_value = MagicMock(
            stripe_id="cus_org_test", user_id=org_member_stripe_customer.user_id
        )
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                # no org_name
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "org_name" in resp.data

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_team_checkout_passes_metadata(
        self,
        mock_get_customer,
        mock_create,
        org_member_client,
        org_member_stripe_customer,
        team_plan,
        team_plan_price,
    ):
        """Team checkout should pass org_name in metadata to Stripe."""
        mock_get_customer.return_value = MagicMock(
            stripe_id="cus_org_test", user_id=org_member_stripe_customer.user_id
        )
        mock_create.return_value = "https://checkout.stripe.com/session"

        org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "quantity": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "My Team Org",
            },
            format="json",
        )
        assert mock_create.call_args.kwargs["metadata"] == {"org_name": "My Team Org"}

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_already_subscribed_user_can_still_create_checkout(
        self,
        mock_get_customer,
        mock_create,
        authed_client,
        plan_price,
        subscription,
        mock_stripe_customer,
    ):
        """A user with an existing active subscription may still open a new
        Checkout Session — e.g. to upgrade or re-subscribe after cancel.
        The view does not guard against duplicate checkouts; Stripe's Billing
        flow handles proration / replacement.
        """
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session-dup"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(plan_price.id),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["url"] == "https://checkout.stripe.com/session-dup"
        mock_create.assert_called_once()

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_display_currency_query_param_does_not_drift_checkout_price(
        self, mock_get_customer, mock_create, authed_client, plan_price, mock_stripe_customer
    ):
        """Catalog display currency (?currency=eur) must not leak into checkout.

        The Stripe price_id is USD-pinned; a drifted display currency on
        the pricing page cannot cause us to quote the user in a currency
        we don't actually charge in.
        """
        ExchangeRate.objects.create(
            currency="eur",
            rate="0.90",
            fetched_at=datetime.now(UTC),
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/?currency=eur",
            {
                "plan_price_id": str(plan_price.id),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 200
        # price_id forwarded verbatim — no currency-converted variant.
        assert mock_create.call_args.kwargs["price_id"] == plan_price.stripe_price_id


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
        assert resp.status_code == 200
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
    def test_portal_response_has_no_location_header(
        self, mock_get_customer, mock_portal, authed_client, mock_stripe_customer
    ):
        mock_get_customer.return_value = mock_stripe_customer
        mock_portal.return_value = "https://billing.stripe.com/portal"

        resp = authed_client.post(
            "/api/v1/billing/portal-sessions/",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert "Location" not in resp

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
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["status"] == "active"

    def test_returns_free_subscription(self, authed_client, free_subscription, free_plan):
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["status"] == "active"
        assert str(resp.data["plan"]["id"]) == str(free_plan.id)

    def test_no_subscription_returns_404(self, authed_client, user):
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestCancelSubscription:
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancels_subscription(self, mock_cancel, authed_client, subscription):
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        # Cancellation takes effect at period end, so the response is 202 Accepted
        # with the still-active subscription echoed back.
        assert resp.status_code == 202
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    def test_free_subscription_returns_404(self, authed_client, free_subscription):
        """Cannot cancel a free-plan subscription via the API."""
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 404

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 404

    def test_no_active_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestUpdateSubscription:
    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_changes_plan(self, mock_change, authed_client, subscription, plan_price):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 200
        mock_change.assert_called_once()
        # The view must resolve the UUID to the underlying Stripe price ID.
        assert mock_change.call_args.kwargs["new_stripe_price_id"] == plan_price.stripe_price_id

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_plan_only_does_not_call_update_seat_count(
        self, mock_change, authed_client, subscription, plan_price
    ):
        with patch("apps.billing.views.update_seat_count", new_callable=AsyncMock) as mock_seats:
            authed_client.patch(
                "/api/v1/billing/subscriptions/me/",
                {"plan_price_id": str(plan_price.id)},
                format="json",
            )
            mock_seats.assert_not_called()
        mock_change.assert_called_once()

    def test_free_subscription_returns_404(self, authed_client, free_subscription, plan_price):
        """Cannot change plan on a free subscription — must go through checkout."""
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 404

    def test_invalid_plan_returns_404(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(uuid4())},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_updates_seats(self, mock_seats, authed_client, team_subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 200
        mock_seats.assert_called_once()
        assert mock_seats.call_args.kwargs["quantity"] == 5

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_does_not_call_change_plan(
        self, mock_seats, authed_client, team_subscription
    ):
        with patch("apps.billing.views.change_plan", new_callable=AsyncMock) as mock_change:
            authed_client.patch(
                "/api/v1/billing/subscriptions/me/",
                {"quantity": 3},
                format="json",
            )
            mock_change.assert_not_called()
        mock_seats.assert_called_once()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_rejected_on_personal_plan(self, mock_seats, authed_client, subscription):
        """Personal plans must not accept multi-seat updates via the seat-only path."""
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 400
        mock_seats.assert_not_called()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_accepted_with_single_seat(
        self, mock_seats, authed_client, team_subscription
    ):
        """Team plans accept a single seat (solo org owner starting a team)."""
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 1},
            format="json",
        )
        assert resp.status_code == 200
        mock_seats.assert_called_once()

    def test_invalid_quantity_returns_400(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 0},
            format="json",
        )
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, authed_client, subscription):
        resp = authed_client.patch("/api/v1/billing/subscriptions/me/", {}, format="json")
        assert resp.status_code == 400

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 404

    def test_customer_without_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"quantity": 5},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_combined_plan_and_seats_update(
        self, mock_change, authed_client, subscription, team_plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(team_plan_price.id), "quantity": 3},
            format="json",
        )
        assert resp.status_code == 200
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
                "/api/v1/billing/subscriptions/me/",
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
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id), "prorate": False},
            format="json",
        )
        assert mock_change.call_args.kwargs["prorate"] is False

    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_true_calls_cancel(self, mock_cancel, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 200
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_false_calls_resume(
        self, mock_resume, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 200
        mock_resume.assert_called_once()

    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_toggle_does_not_call_change_plan(
        self, mock_cancel, mock_resume, authed_client, subscription
    ):
        with patch("apps.billing.views.change_plan", new_callable=AsyncMock) as mock_change:
            authed_client.patch(
                "/api/v1/billing/subscriptions/me/",
                {"cancel_at_period_end": True},
                format="json",
            )
            mock_change.assert_not_called()

    def test_cancel_at_period_end_with_plan_returns_400(
        self, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id), "cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 400

    def test_cancel_at_period_end_free_subscription_returns_404(
        self, authed_client, free_subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 404

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/billing/subscriptions/me/", {"quantity": 5}, format="json")
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
        match = next(p for p in resp.data["results"] if p["name"] == "100 Credits")
        assert match["credits"] == 100
        assert match["type"] == "one_time"
        assert match["price"]["amount"] == 999

    def test_excludes_inactive_products(self, authed_client, product, product_price):
        product.is_active = False
        product.save()
        resp = authed_client.get("/api/v1/billing/products/")
        assert resp.status_code == 200
        assert not any(p["name"] == "100 Credits" for p in resp.data["results"])

    def test_response_includes_display_amount_and_currency(
        self, authed_client, product, product_price
    ):
        resp = authed_client.get("/api/v1/billing/products/")
        match = next(p for p in resp.data["results"] if p["name"] == "100 Credits")
        assert match["price"]["currency"] == "usd"
        assert match["price"]["display_amount"] == 9.99

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
    def test_team_plan_with_single_seat_succeeds(
        self, mock_get_customer, mock_create, org_member_client, mock_stripe_customer, db
    ):
        team_plan = Plan.objects.create(
            name="Team Mini", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_mini", amount=1500
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
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
        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["quantity"] == 1

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
        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["quantity"] == 2


@pytest.mark.django_db
class TestUpdateSubscriptionQuantityValidation:
    """Quantity-rule validation through PATCH /subscription/."""

    def test_personal_plan_with_quantity_gt_1_returns_400(
        self, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id), "quantity": 2},
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
        price = resp.data["results"][0]["price"]
        assert price["currency"] == "eur"
        # 999 cents * 0.91 = 909.09 → round → 909 minor units → 9.09 → friendly → 8.99
        # (nearest of {8.99, 9.49, 9.99}; 8.99 is 0.10 away)
        assert price["display_amount"] == 8.99
        assert price["approximate"] is True
        # Original USD cents still present
        assert price["amount"] == 999

    def test_falls_back_to_usd_when_rate_missing(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=eur")
        price = resp.data["results"][0]["price"]
        # No ExchangeRate for EUR → fallback to USD
        assert price["currency"] == "usd"
        assert price["display_amount"] == 9.99
        assert price["approximate"] is False

    def test_invalid_currency_returns_400(self, plan, plan_price):
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=xyz")
        assert resp.status_code == 400

    def test_authenticated_user_preferred_currency(self, authed_client, user, plan, plan_price):
        ExchangeRate.objects.create(
            currency="gbp",
            rate="0.79",
            fetched_at=datetime.now(UTC),
        )
        user.preferred_currency = "gbp"
        user.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data["results"][0]["price"]["currency"] == "gbp"

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
        assert resp.data["results"][0]["price"]["currency"] == "eur"

    def test_zero_decimal_currency_conversion(self, plan, plan_price):
        ExchangeRate.objects.create(
            currency="jpy",
            rate="149.5",
            fetched_at=datetime.now(UTC),
        )
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=jpy")
        price = resp.data["results"][0]["price"]
        assert price["currency"] == "jpy"
        # 999 * 149.5 = 149350.5 → round → 149350 → zero-decimal → friendly → 149400.0
        assert price["display_amount"] == 149400.0
        assert price["approximate"] is True

    def test_subscription_includes_currency(self, authed_client, subscription):
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
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
        price = resp.data["results"][0]["price"]
        assert price["currency"] == "eur"
        # 500 * 0.91 = 455 → /100 → 4.55 → friendly → 4.49
        # (nearest of {3.99, 4.49, 4.99}; 4.49 is 0.06 away)
        assert price["display_amount"] == 4.49
        assert price["approximate"] is True

    def test_user_default_currency_returns_usd(self, authed_client, user, plan, plan_price):
        """User with default preferred_currency='usd' gets USD without exchange rate lookup."""
        assert user.preferred_currency == "usd"
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data["results"][0]["price"]["currency"] == "usd"

    def test_user_unsupported_preferred_currency_falls_back_to_usd(
        self, authed_client, user, plan, plan_price
    ):
        """User with a preferred_currency not in SUPPORTED_CURRENCIES should get USD."""
        user.preferred_currency = "xyz"
        user.save()
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.data["results"][0]["price"]["currency"] == "usd"

    def test_empty_currency_param_ignored(self, plan, plan_price):
        """?currency= (empty string) should fall back to USD."""
        client = APIClient()
        resp = client.get("/api/v1/billing/plans/?currency=")
        assert resp.data["results"][0]["price"]["currency"] == "usd"


# ---------------------------------------------------------------------------
# Team subscription resolution + billing-authority gate on mutations
# ---------------------------------------------------------------------------


@pytest.fixture
def team_org_setup(org_member_user, team_plan, team_plan_price):
    """Active org owned by an org_member user, with a team StripeCustomer and
    an active team Subscription. ``org_member_user`` is both OWNER and
    is_billing=True, matching how ``_create_org_with_owner`` seeds new orgs."""
    from apps.billing.models import StripeCustomer, Subscription
    from apps.orgs.models import Org, OrgMember, OrgRole

    org = Org.objects.create(name="Authz Org", slug="authz-org", created_by=org_member_user)
    OrgMember.objects.create(
        org=org,
        user=org_member_user,
        role=OrgRole.OWNER,
        is_billing=True,
    )
    customer = StripeCustomer.objects.create(stripe_id="cus_team_authz", org=org, livemode=False)
    subscription = Subscription.objects.create(
        stripe_id="sub_team_authz",
        stripe_customer=customer,
        status="active",
        plan=team_plan,
        quantity=3,
        current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    return org, customer, subscription


@pytest.mark.django_db
class TestTeamSubscriptionResolution:
    def test_billing_member_get_returns_team_subscription(
        self, org_member_client, team_org_setup, team_plan
    ):
        _, _, sub = team_org_setup
        resp = org_member_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert str(resp.data["plan"]["id"]) == str(team_plan.id)
        assert str(resp.data["id"]) == str(sub.id)

    def test_non_billing_member_get_still_returns_team_subscription(
        self, team_org_setup, team_plan
    ):
        """Read access to the team sub is granted to ANY active org member —
        only mutations require is_billing=True."""
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import AccountType, User

        org, _, _ = team_org_setup
        member_user = User.objects.create_user(
            email="plain@example.com",
            full_name="Plain Member",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=member_user, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member_user)

        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert str(resp.data["plan"]["id"]) == str(team_plan.id)

    def test_org_member_without_membership_returns_404(self, org_member_client):
        """An org_member user who isn't a member of any active org has no sub
        to look up."""
        resp = org_member_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestBillingAuthorityOnMutations:
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_billing_member_can_delete(self, mock_cancel, org_member_client, team_org_setup):
        resp = org_member_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 202
        mock_cancel.assert_called_once()

    def test_non_billing_member_delete_returns_403(self, team_org_setup):
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import AccountType, User

        org, _, _ = team_org_setup
        member = User.objects.create_user(
            email="nb-del@example.com",
            full_name="NB Del",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 403

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_billing_member_can_patch_plan(
        self, mock_change, org_member_client, team_org_setup, team_plan_price
    ):
        resp = org_member_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(team_plan_price.id), "quantity": 3},
            format="json",
        )
        assert resp.status_code == 200
        mock_change.assert_called_once()

    def test_non_billing_member_patch_returns_403(self, team_org_setup, team_plan_price):
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import AccountType, User

        org, _, _ = team_org_setup
        member = User.objects.create_user(
            email="nb-patch@example.com",
            full_name="NB Patch",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(team_plan_price.id), "quantity": 3},
            format="json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestCancelNoticeEmail:
    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_delete_sends_scheduled_notice(
        self, _mock_cancel, mock_task, authed_client, subscription
    ):
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 202
        mock_task.delay.assert_called_once()
        recipients, label, action = mock_task.delay.call_args.args
        assert recipients == ["billing@example.com"]
        assert action == "scheduled"
        assert label == subscription.plan.name

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_patch_cancel_at_period_end_true_sends_scheduled(
        self, _mock_cancel, mock_task, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 200
        assert mock_task.delay.call_args.args[2] == "scheduled"

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    def test_patch_cancel_at_period_end_false_sends_resumed(
        self, _mock_resume, mock_task, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 200
        assert mock_task.delay.call_args.args[2] == "resumed"

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_patch_plan_change_does_not_send_notice(
        self, _mock_change, mock_task, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 200
        mock_task.delay.assert_not_called()

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_team_delete_notifies_every_billing_member(
        self, _mock_cancel, mock_task, org_member_client, team_org_setup
    ):
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import AccountType, User

        org, _, _ = team_org_setup
        extra_billing = User.objects.create_user(
            email="finance@example.com",
            full_name="Finance",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=extra_billing, role=OrgRole.MEMBER, is_billing=True)
        non_billing = User.objects.create_user(
            email="eng@example.com",
            full_name="Eng",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=non_billing, role=OrgRole.MEMBER, is_billing=False)

        resp = org_member_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 202
        recipients = mock_task.delay.call_args.args[0]
        assert set(recipients) == {"orgowner@example.com", "finance@example.com"}


# ---------------------------------------------------------------------------
# Product checkout (POST /api/v1/billing/product-checkout-sessions/)
# ---------------------------------------------------------------------------


@pytest.fixture
def boost_product(db):
    from apps.billing.models import Product, ProductPrice, ProductType

    product = Product.objects.create(
        name="50 Credits", type=ProductType.ONE_TIME, credits=50, is_active=True
    )
    ProductPrice.objects.create(product=product, stripe_price_id="price_boost_50", amount=499)
    return product


@pytest.fixture
def boost_product_price(boost_product):
    return boost_product.price


@pytest.mark.django_db
class TestProductCheckoutPersonal:
    @patch("apps.billing.views.create_product_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_personal_user_can_purchase(
        self,
        mock_customer,
        mock_session,
        authed_client,
        boost_product,
        boost_product_price,
        mock_stripe_customer,
    ):
        mock_customer.return_value = mock_stripe_customer
        mock_session.return_value = "https://checkout.stripe.com/product"

        resp = authed_client.post(
            "/api/v1/billing/product-checkout-sessions/",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["url"] == "https://checkout.stripe.com/product"
        metadata = mock_session.call_args.kwargs["metadata"]
        assert metadata == {"product_id": str(boost_product.id)}
        assert mock_session.call_args.kwargs["price_id"] == "price_boost_50"

    def test_invalid_product_price_returns_404(self, authed_client):
        resp = authed_client.post(
            "/api/v1/billing/product-checkout-sessions/",
            {
                "product_price_id": str(uuid4()),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestProductCheckoutTeamOwnership:
    def _setup_org(self, role, is_billing=False):
        from apps.orgs.models import Org, OrgMember
        from apps.users.models import AccountType, User

        user = User.objects.create_user(
            email=f"{role.value}@example.com",
            full_name=f"{role.value} User",
            account_type=AccountType.ORG_MEMBER,
        )
        org = Org.objects.create(
            name="Team Org", slug="team-org", created_by=user, is_active=True
        )
        OrgMember.objects.create(org=org, user=user, role=role, is_billing=is_billing)
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, client

    @patch("apps.billing.views.create_product_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_org_owner_can_purchase_and_metadata_carries_org_id(
        self, mock_customer, mock_session, boost_product, boost_product_price, mock_stripe_customer
    ):
        from apps.orgs.models import OrgRole

        _, org, client = self._setup_org(OrgRole.OWNER, is_billing=True)
        mock_customer.return_value = mock_stripe_customer
        mock_session.return_value = "https://checkout.stripe.com/team-product"

        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        metadata = mock_session.call_args.kwargs["metadata"]
        assert metadata == {"product_id": str(boost_product.id), "org_id": str(org.id)}
        # Customer resolution must use org_id, not user_id, so credits bill to the org customer.
        assert mock_customer.call_args.kwargs.get("org_id") == org.id
        assert "user_id" not in mock_customer.call_args.kwargs

    def test_org_admin_cannot_purchase(self, boost_product_price):
        from apps.orgs.models import OrgRole

        _, _, client = self._setup_org(OrgRole.ADMIN)
        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 403

    def test_org_member_cannot_purchase(self, boost_product_price):
        from apps.orgs.models import OrgRole

        _, _, client = self._setup_org(OrgRole.MEMBER)
        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/billing/credits/me/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreditBalanceView:
    def test_personal_user_gets_zero_by_default(self, authed_client, user):
        resp = authed_client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balance": 0, "scope": "user"}

    def test_personal_user_sees_own_balance(self, authed_client, user):
        from apps.billing.models import CreditBalance

        CreditBalance.objects.create(user=user, balance=125)
        resp = authed_client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balance": 125, "scope": "user"}

    def test_org_member_sees_org_balance(self, org_member_user, team_org_setup):
        from apps.billing.models import CreditBalance

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=500)
        client = APIClient()
        client.force_authenticate(user=org_member_user)
        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balance": 500, "scope": "org"}

    def test_non_billing_member_still_sees_org_balance(self, team_org_setup):
        """Read access to the org's credit balance is granted to any member,
        consistent with /subscriptions/me/ read semantics."""
        from apps.billing.models import CreditBalance
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import AccountType, User

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=42)
        member = User.objects.create_user(
            email="plain-credits@example.com",
            full_name="Plain",
            account_type=AccountType.ORG_MEMBER,
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balance": 42, "scope": "org"}

    def test_org_member_without_membership_returns_404(self, org_member_client):
        resp = org_member_client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 404
