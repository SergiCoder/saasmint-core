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

    def test_personal_user_sees_all_plans(
        self, authed_client, plan, plan_price, team_plan, team_plan_price
    ):
        # Users without an owned org can upgrade to a team plan via team-context
        # checkout, so both contexts must be discoverable from the listing endpoint.
        resp = authed_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 2
        assert {row["context"] for row in resp.data["results"]} == {"personal", "team"}

    def test_org_member_sees_all_plans(
        self, org_member_client, plan, plan_price, team_plan, team_plan_price
    ):
        # Org members see the same catalogue as everyone else; the
        # checkout endpoint enforces which contexts they can actually purchase.
        resp = org_member_client.get("/api/v1/billing/plans/")
        assert resp.status_code == 200
        assert len(resp.data["results"]) == 2
        assert {row["context"] for row in resp.data["results"]} == {"personal", "team"}


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
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_trial_suppressed_for_team_plans(
        self, mock_team_customer, mock_create, org_member_client, db
    ):
        team_plan = Plan.objects.create(
            name="Team Monthly", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team", amount=2999
        )
        mock_team_customer.return_value = "cus_team_fresh"
        mock_create.return_value = "https://checkout.stripe.com/session"

        org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "seat_limit": 2,
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

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_personal_user_can_checkout_team_plan_when_no_owned_org(
        self,
        mock_team_customer,
        mock_create,
        authed_client,
        team_plan,
        team_plan_price,
    ):
        """PR 5: a user without an owned org may upgrade to team. The
        eventual ``checkout.session.completed`` webhook creates the org and
        the OrgMember row. The 409 is reserved for users who already own
        one."""
        mock_team_customer.return_value = "cus_team_personal_upgrade"
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = authed_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "My Org",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["stripe_customer_id"] == "cus_team_personal_upgrade"

    def test_user_owning_org_cannot_create_second_org(
        self, org_member_client, org_member_user, team_plan, team_plan_price
    ):
        """Rule 8: one owned org per user. A user who already owns an org
        cannot start a second team checkout."""
        from apps.orgs.models import Org, OrgMember, OrgRole

        org = Org.objects.create(name="Existing", slug="existing", created_by=org_member_user)
        OrgMember.objects.create(org=org, user=org_member_user, role=OrgRole.OWNER, is_billing=True)

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Second Org",
            },
            format="json",
        )
        assert resp.status_code == 409
        assert resp.data["code"] == "org_already_owned"
        assert "already own" in resp.data["detail"].lower()

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_org_member_can_checkout_personal_plan(
        self,
        mock_get_customer,
        mock_create,
        org_member_client,
        org_member_user,
        plan_price,
        mock_stripe_customer,
    ):
        """Rule 5b: dropping ``User.account_type`` removes the gate that
        previously rejected ORG_MEMBER callers from personal-plan checkout.
        An org member (any role) may now hold a personal sub concurrently
        with their team sub — the personal checkout succeeds and routes to
        the user's own Stripe customer."""
        from apps.orgs.models import Org, OrgMember, OrgRole

        org = Org.objects.create(name="HoldsTeam", slug="holds-team", created_by=org_member_user)
        OrgMember.objects.create(org=org, user=org_member_user, role=OrgRole.OWNER, is_billing=True)
        mock_get_customer.return_value = mock_stripe_customer
        mock_create.return_value = "https://checkout.stripe.com/personal-from-org-member"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(plan_price.id),
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["url"] == "https://checkout.stripe.com/personal-from-org-member"
        # Personal-plan checkout must resolve to the caller's user-scoped
        # customer, not the org's — even when the caller is an org owner.
        assert mock_get_customer.call_args.kwargs.get("user_id") == org_member_user.id
        assert "org_id" not in mock_get_customer.call_args.kwargs

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_team_checkout_requires_org_name(
        self,
        mock_team_customer,
        mock_create,
        org_member_client,
        team_plan,
        team_plan_price,
    ):
        """Team plan checkout must include org_name."""
        mock_team_customer.return_value = "cus_team_org_test"
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                # no org_name
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "org_name" in resp.data

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_team_checkout_passes_metadata(
        self,
        mock_team_customer,
        mock_create,
        org_member_client,
        team_plan,
        team_plan_price,
    ):
        """Team checkout passes org_name + keep_personal_subscription in metadata.
        Stripe metadata values are strings — booleans go through as
        ``"true"``/``"false"`` and the webhook parses them back."""
        mock_team_customer.return_value = "cus_team_org_test"
        mock_create.return_value = "https://checkout.stripe.com/session"

        org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "My Team Org",
            },
            format="json",
        )
        assert mock_create.call_args.kwargs["metadata"] == {
            "org_name": "My Team Org",
            "keep_personal_subscription": "false",
        }

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_team_checkout_keep_personal_subscription_true_propagates(
        self,
        mock_team_customer,
        mock_create,
        org_member_client,
        team_plan,
        team_plan_price,
    ):
        """Opt-out: ``keep_personal_subscription=True`` is encoded as
        ``"true"`` so the webhook keeps the personal sub running (rule 5b)."""
        mock_team_customer.return_value = "cus_team_keep"
        mock_create.return_value = "https://checkout.stripe.com/session"

        org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_plan_price.id),
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Keep Personal",
                "keep_personal_subscription": True,
            },
            format="json",
        )
        assert mock_create.call_args.kwargs["metadata"]["keep_personal_subscription"] == "true"

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
class TestPortalSessionContextRouting:
    """``?context=personal|team`` routes the portal session to the right
    Stripe customer (rule 5a/5b). Without this, a concurrent biller clicking
    ``Manage billing`` on the team card would silently land on their personal
    customer and never see the team sub."""

    def _setup_team_member(self, *, role, is_billing: bool, label: str):
        """Create a user + org + team customer + authed client. ``label``
        keeps emails/slugs unique across tests in this class."""
        from apps.billing.models import StripeCustomer
        from apps.orgs.models import Org, OrgMember
        from apps.users.models import User

        user = User.objects.create_user(
            email=f"portal-{label}@example.com", full_name=f"Portal {label}"
        )
        org = Org.objects.create(name=f"PortalOrg{label}", slug=f"portal-{label}", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=role, is_billing=is_billing)
        team_customer = StripeCustomer.objects.create(
            stripe_id=f"cus_team_portal_{label}", org=org, livemode=False
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, team_customer, client

    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    def test_team_context_routes_to_team_customer(self, mock_portal):
        """``?context=team`` for an is_billing owner opens the team
        portal — the team customer's stripe_id is forwarded, not the
        user's personal customer."""
        from apps.orgs.models import OrgRole

        _, _, team_customer, client = self._setup_team_member(
            role=OrgRole.OWNER, is_billing=True, label="owner"
        )
        mock_portal.return_value = "https://billing.stripe.com/team-portal"

        resp = client.post(
            "/api/v1/billing/portal-sessions/?context=team",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["url"] == "https://billing.stripe.com/team-portal"
        assert mock_portal.call_args.kwargs["stripe_customer_id"] == team_customer.stripe_id

    def test_team_context_403_for_non_billing_member(self):
        """An admin or member without ``is_billing=True`` cannot open the
        team portal — same gate as cancel/resume on the team sub."""
        from apps.orgs.models import OrgRole

        _, _, _, client = self._setup_team_member(
            role=OrgRole.ADMIN, is_billing=False, label="admin"
        )

        resp = client.post(
            "/api/v1/billing/portal-sessions/?context=team",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 403

    def test_team_context_404_when_no_team_customer(self):
        """An is_billing owner whose org has no ``StripeCustomer`` row yet
        (subscription wasn't created) gets 404 — we must not auto-mint a
        personal customer for a team-context request."""
        from apps.billing.models import StripeCustomer
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        user = User.objects.create_user(
            email="portal-no-cust@example.com", full_name="No Cust"
        )
        org = Org.objects.create(name="NoCustOrg", slug="portal-no-cust", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER, is_billing=True)
        # No StripeCustomer row for this org.
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.post(
            "/api/v1/billing/portal-sessions/?context=team",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 404
        # Sanity: no personal customer was leaked into the wrong scope.
        assert not StripeCustomer.objects.filter(user=user).exists()

    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_personal_context_explicit_routes_to_personal_customer(
        self, mock_get_customer, mock_portal, mock_stripe_customer
    ):
        """An org member explicitly passing ``?context=personal`` opens
        their own personal portal even when a team customer also exists
        (rule 5b — concurrent biller managing their personal sub)."""
        from apps.orgs.models import OrgRole

        user, _, _, client = self._setup_team_member(
            role=OrgRole.OWNER, is_billing=True, label="concurrent"
        )
        mock_get_customer.return_value = mock_stripe_customer
        mock_portal.return_value = "https://billing.stripe.com/personal-portal"

        resp = client.post(
            "/api/v1/billing/portal-sessions/?context=personal",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["url"] == "https://billing.stripe.com/personal-portal"
        # get_or_create_customer was called with user_id (personal scope),
        # not org_id — confirms no scope mixing.
        assert mock_get_customer.call_args.kwargs["user_id"] == user.id

    @patch("apps.billing.views.create_billing_portal_session", new_callable=AsyncMock)
    def test_default_routing_for_org_member_picks_team(self, mock_portal):
        """Same default as cancel/resume: an org member without
        ``?context=`` lands on the team portal."""
        from apps.orgs.models import OrgRole

        _, _, team_customer, client = self._setup_team_member(
            role=OrgRole.OWNER, is_billing=True, label="default"
        )
        mock_portal.return_value = "https://billing.stripe.com/team-default"

        resp = client.post(
            "/api/v1/billing/portal-sessions/",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 200
        assert mock_portal.call_args.kwargs["stripe_customer_id"] == team_customer.stripe_id

    def test_invalid_context_value_returns_400(self, authed_client):
        """``?context=`` accepts only ``personal`` / ``team`` / empty."""
        resp = authed_client.post(
            "/api/v1/billing/portal-sessions/?context=enterprise",
            {"return_url": "https://localhost/dashboard"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestSubscriptionView:
    def test_returns_active_subscription(self, authed_client, subscription):
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert resp.data["results"][0]["status"] == "active"

    def test_response_surfaces_cancel_at_when_scheduled(self, authed_client, subscription):
        """End-to-end: a subscription scheduled to cancel (mirror of Stripe's
        ``cancel_at`` set by the webhook) exposes the timestamp on the API
        response. Unit-level coverage in ``test_serializers.py`` only asserts
        the field is declared with default ``None`` on a fresh row; this test
        proves the populated value reaches the wire through the GET endpoint."""
        scheduled = datetime(2026, 2, 1, tzinfo=UTC)
        subscription.cancel_at = scheduled
        subscription.save(update_fields=["cancel_at"])

        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert resp.data["results"][0]["cancel_at"] is not None
        # DRF serializes datetimes to ISO-8601 strings; assert the field round-trips.
        from rest_framework.fields import DateTimeField

        assert resp.data["results"][0]["cancel_at"] == DateTimeField().to_representation(scheduled)

    def test_no_subscription_returns_empty_list(self, authed_client, user):
        """PR 5: free tier is now an empty list (was 404). Single-sub callers
        adapt by checking ``count == 0`` instead of catching the 404."""
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0
        assert resp.data["results"] == []

    def test_personal_user_with_customer_but_no_subscription_returns_empty_list(
        self, authed_client, stripe_customer
    ):
        """``_get_active_subscriptions_for_user`` non-org-member path: the
        user has a ``StripeCustomer`` row (so ``customer_id is not None`` and
        the ``stripe_customer_id``-indexed lookup is exercised) but no active
        Subscription exists on either the user or the customer. Both
        ``sub_user`` and ``sub_customer`` resolve to ``None`` and the function
        returns an empty list rather than raising. Sister to
        ``test_no_subscription_returns_empty_list`` which covers the
        no-customer-at-all branch (``customer_id is None``)."""
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0
        assert resp.data["results"] == []

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code in (401, 403)

    def test_get_returns_empty_list_after_cancellation_webhook(
        self, authed_client, user, subscription
    ):
        """End-to-end: after a ``customer.subscription.deleted`` webhook is
        processed, the API reports no active subscription. The Subscription
        row stays in CANCELED state for history but ``GET /me/`` returns an
        empty list (the new shape — was 404 pre-PR 5).

        Previously the cancellation handler created a free fallback row, so a
        follow-up GET would still return 200 with active state; the
        ``Subscription``-as-Stripe-mirror refactor removed that fallback. The
        unit tests in core verify the row state — this integration test
        asserts the absence is observable through the API."""
        from asgiref.sync import async_to_sync
        from saasmint_core.services.webhooks import _on_subscription_deleted

        from apps.billing.models import Subscription
        from apps.billing.repositories import get_webhook_repos

        # Mirror the production shape for personal paid subs: webhook sync
        # writes user_id directly on the Subscription row (from customer.user_id)
        # so the deleted handler can take the personal-user branch.
        subscription.user = user
        subscription.save(update_fields=["user"])

        # Sanity-check: the API sees the active sub before cancellation.
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1

        repos = get_webhook_repos()
        async_to_sync(_on_subscription_deleted)({"id": subscription.stripe_id}, repos)

        # The Subscription row still exists in CANCELED state for history,
        # but the API resolves "current subscription" via active statuses
        # only — so the user has no active subscription.
        canceled = Subscription.objects.get(id=subscription.id)
        assert canceled.status == "canceled"

        # And no fallback Subscription was created for the user.
        assert Subscription.objects.filter(user=user).count() == 1

        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0


@pytest.mark.django_db
class TestCancelSubscription:
    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancels_subscription(self, mock_cancel, _mock_task, authed_client, subscription):
        resp = authed_client.delete("/api/v1/billing/subscriptions/me/")
        # Cancellation takes effect at period end, so the response is 202 Accepted
        # with the still-active subscription echoed back.
        assert resp.status_code == 202
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

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
            {"seat_limit": 5},
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
                {"seat_limit": 3},
                format="json",
            )
            mock_change.assert_not_called()
        mock_seats.assert_called_once()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seats_only_rejected_on_personal_plan(self, mock_seats, authed_client, subscription):
        """Personal plans must not accept multi-seat updates via the seat-only path."""
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"seat_limit": 5},
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
            {"seat_limit": 1},
            format="json",
        )
        assert resp.status_code == 200
        mock_seats.assert_called_once()

    def test_invalid_quantity_returns_400(self, authed_client, subscription):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"seat_limit": 0},
            format="json",
        )
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, authed_client, subscription):
        resp = authed_client.patch("/api/v1/billing/subscriptions/me/", {}, format="json")
        assert resp.status_code == 400

    def test_no_customer_returns_404(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"seat_limit": 5},
            format="json",
        )
        assert resp.status_code == 404

    def test_customer_without_subscription_returns_404(self, authed_client, stripe_customer):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"seat_limit": 5},
            format="json",
        )
        assert resp.status_code == 404

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_combined_plan_and_seats_update(
        self, mock_change, authed_client, subscription, team_plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(team_plan_price.id), "seat_limit": 3},
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
                {"plan_price_id": str(team_plan_price.id), "seat_limit": 3},
                format="json",
            )
            mock_seats.assert_not_called()
        mock_change.assert_called_once()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seat_only_reduction_below_member_count_rejected(
        self, mock_seats, team_plan, team_plan_price
    ):
        """Reducing seats below the org's current head-count must 400 with
        ``code=seats_below_member_count`` — otherwise the sub would bill for
        fewer seats than members actually filled."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="seat-floor@example.com", full_name="Floor")
        org = Org.objects.create(name="FloorOrg", slug="floor-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)
        for i in range(2):
            extra = User.objects.create_user(email=f"m{i}@floor.com", full_name=f"M{i}")
            OrgMember.objects.create(org=org, user=extra, role=OrgRole.MEMBER)
        team_customer = StripeCustomer.objects.create(
            stripe_id="cus_floor_team", org=org, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_floor_team",
            stripe_customer=team_customer,
            status="active",
            plan=team_plan,
            seat_limit=5,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        client = APIClient()
        client.force_authenticate(user=owner)
        # 3 members; attempt to drop seats to 2 → reject.
        resp = client.patch(
            "/api/v1/billing/subscriptions/me/?context=team",
            {"seat_limit": 2},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.data["code"] == "seats_below_member_count"
        mock_seats.assert_not_called()

    @patch("apps.billing.views.update_seat_count", new_callable=AsyncMock)
    def test_seat_only_reduction_at_member_count_allowed(
        self, mock_seats, team_plan, team_plan_price
    ):
        """Reducing to exactly the current member count is allowed — every
        seat is still filled, none are over-committed."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="seat-eq@example.com", full_name="Eq")
        org = Org.objects.create(name="EqOrg", slug="eq-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)
        team_customer = StripeCustomer.objects.create(
            stripe_id="cus_eq_team", org=org, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_eq_team",
            stripe_customer=team_customer,
            status="active",
            plan=team_plan,
            seat_limit=5,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        client = APIClient()
        client.force_authenticate(user=owner)
        # 1 member (the owner); drop to 1 → allowed.
        resp = client.patch(
            "/api/v1/billing/subscriptions/me/?context=team",
            {"seat_limit": 1},
            format="json",
        )
        assert resp.status_code == 200
        mock_seats.assert_called_once()

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_combined_plan_seat_reduction_below_member_count_rejected(
        self, mock_change, team_plan, team_plan_price
    ):
        """Same guard applies on the combined plan+seat path — a downgrade
        with seats below member count must 400 before any Stripe call."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="seat-combo@example.com", full_name="Combo")
        org = Org.objects.create(name="ComboOrg", slug="combo-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)
        for i in range(2):
            extra = User.objects.create_user(email=f"c{i}@combo.com", full_name=f"C{i}")
            OrgMember.objects.create(org=org, user=extra, role=OrgRole.MEMBER)
        team_customer = StripeCustomer.objects.create(
            stripe_id="cus_combo_team", org=org, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_combo_team",
            stripe_customer=team_customer,
            status="active",
            plan=team_plan,
            seat_limit=5,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        client = APIClient()
        client.force_authenticate(user=owner)
        # 3 members; combined patch attempting seats=2 → reject.
        resp = client.patch(
            "/api/v1/billing/subscriptions/me/?context=team",
            {"plan_price_id": str(team_plan_price.id), "seat_limit": 2},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.data["code"] == "seats_below_member_count"
        mock_change.assert_not_called()

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

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_true_calls_cancel(
        self, mock_cancel, _mock_task, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 200
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.kwargs["at_period_end"] is True

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    def test_cancel_at_period_end_false_calls_resume(
        self, mock_resume, _mock_task, authed_client, subscription
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"cancel_at_period_end": False},
            format="json",
        )
        assert resp.status_code == 200
        mock_resume.assert_called_once()

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.resume_subscription", new_callable=AsyncMock)
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_cancel_toggle_does_not_call_change_plan(
        self, mock_cancel, mock_resume, _mock_task, authed_client, subscription
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

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/billing/subscriptions/me/", {"seat_limit": 5}, format="json")
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
                "seat_limit": 2,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
            },
            format="json",
        )
        assert resp.status_code == 400

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_team_plan_with_single_seat_succeeds(
        self, mock_team_customer, mock_create, org_member_client, db
    ):
        team_plan = Plan.objects.create(
            name="Team Mini", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_mini", amount=1500
        )
        mock_team_customer.return_value = "cus_team_mini"
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "seat_limit": 1,
                "success_url": "https://localhost/success",
                "cancel_url": "https://localhost/cancel",
                "org_name": "Mini Org",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["quantity"] == 1

    @patch("apps.billing.views.create_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.create_team_stripe_customer", new_callable=AsyncMock)
    def test_team_plan_with_min_seats_succeeds(
        self, mock_team_customer, mock_create, org_member_client, db
    ):
        team_plan = Plan.objects.create(
            name="Team Min", context="team", interval="month", is_active=True
        )
        team_price = PlanPrice.objects.create(
            plan=team_plan, stripe_price_id="price_team_min", amount=2000
        )
        mock_team_customer.return_value = "cus_team_min"
        mock_create.return_value = "https://checkout.stripe.com/session"

        resp = org_member_client.post(
            "/api/v1/billing/checkout-sessions/",
            {
                "plan_price_id": str(team_price.id),
                "seat_limit": 2,
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
            {"plan_price_id": str(plan_price.id), "seat_limit": 2},
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
        price = resp.data["results"][0]["plan"]["price"]
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
        seat_limit=3,
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
        assert resp.data["count"] == 1
        assert str(resp.data["results"][0]["plan"]["id"]) == str(team_plan.id)
        assert str(resp.data["results"][0]["id"]) == str(sub.id)

    def test_non_billing_member_get_still_returns_team_subscription(
        self, team_org_setup, team_plan
    ):
        """Read access to the team sub is granted to ANY active org member —
        only mutations require is_billing=True."""
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import User

        org, _, _ = team_org_setup
        member_user = User.objects.create_user(
            email="plain@example.com",
            full_name="Plain Member",
        )
        OrgMember.objects.create(org=org, user=member_user, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member_user)

        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 1
        assert str(resp.data["results"][0]["plan"]["id"]) == str(team_plan.id)

    def test_org_member_without_membership_returns_empty_list(self, org_member_client):
        """A user with no OrgMember row and no personal sub: nothing to
        surface — empty list, no error."""
        resp = org_member_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 0


@pytest.mark.django_db
class TestConcurrentSubscriptions:
    """Rule 5b (keep-personal opt-out during personal→team upgrade): an
    org-member user can hold both a team sub (on the org's customer) and a
    personal sub (on their user-scoped customer) at the same time. The
    /me/ endpoint must surface both."""

    def _setup_concurrent(self, team_plan, plan):
        """Create an org-member user owning an org with a team sub AND a
        user-scoped Stripe customer carrying an active personal sub."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        user = User.objects.create_user(
            email="concurrent@example.com",
            full_name="Concurrent",
        )
        org = Org.objects.create(name="ConcurrentOrg", slug="concurrent-org", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER, is_billing=True)
        team_customer = StripeCustomer.objects.create(
            stripe_id="cus_concurrent_team", org=org, livemode=False
        )
        team_sub = Subscription.objects.create(
            stripe_id="sub_concurrent_team",
            stripe_customer=team_customer,
            status="active",
            plan=team_plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        personal_customer = StripeCustomer.objects.create(
            stripe_id="cus_concurrent_personal", user=user, livemode=False
        )
        personal_sub = Subscription.objects.create(
            stripe_id="sub_concurrent_personal",
            stripe_customer=personal_customer,
            user=user,
            status="active",
            plan=plan,
            seat_limit=1,
            current_period_start=datetime(2025, 12, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return user, team_sub, personal_sub

    def test_get_returns_both_team_and_personal_subs(
        self, plan, plan_price, team_plan, team_plan_price
    ):
        user, team_sub, personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 2
        ids = {str(r["id"]) for r in resp.data["results"]}
        assert ids == {str(team_sub.id), str(personal_sub.id)}

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_delete_with_context_personal_targets_personal_sub(
        self, mock_cancel, _mock_task, plan, plan_price, team_plan, team_plan_price
    ):
        """Concurrent-billing user explicitly cancels their personal sub via
        ``?context=personal``. The cancel call must hit the personal Stripe
        customer's UUID, not the org customer's."""
        user, _team_sub, personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/billing/subscriptions/me/?context=personal")
        assert resp.status_code == 202
        mock_cancel.assert_called_once()
        passed_customer_id = mock_cancel.call_args.kwargs["stripe_customer_id"]
        assert passed_customer_id == personal_sub.stripe_customer_id

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_delete_default_context_targets_team_for_org_member(
        self, mock_cancel, _mock_task, plan, plan_price, team_plan, team_plan_price
    ):
        """Backwards-compat: an org-member user with no ``?context=`` query
        param defaults to the team sub (existing behavior)."""
        user, team_sub, _personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 202
        mock_cancel.assert_called_once()
        passed_customer_id = mock_cancel.call_args.kwargs["stripe_customer_id"]
        assert passed_customer_id == team_sub.stripe_customer_id

    def test_delete_with_invalid_context_returns_400(
        self, plan, plan_price, team_plan, team_plan_price
    ):
        user, _team_sub, _personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/billing/subscriptions/me/?context=invalid")
        assert resp.status_code == 400
        assert "context" in resp.data

    def test_patch_with_invalid_context_returns_400(
        self, plan, plan_price, team_plan, team_plan_price
    ):
        """Parallel of the DELETE invalid-context test for the PATCH path —
        ``_validate_subscription_context`` runs on both endpoints, so a
        bogus ``?context=`` value must reject the PATCH request before any
        billing-authority check or Stripe call can fire."""
        user, _team_sub, _personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.patch(
            "/api/v1/billing/subscriptions/me/?context=garbage",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 400
        assert "context" in resp.data

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_delete_with_empty_context_falls_back_to_default(
        self, mock_cancel, _mock_task, plan, plan_price, team_plan, team_plan_price
    ):
        """Empty-string ``?context=`` (e.g. an HTML form submitting an
        unset value) must be treated as no override —
        ``_validate_subscription_context`` returns ``None`` for ``""`` and
        the default-resolver picks team for an org-member caller. Without
        the empty-string short-circuit the value would fail validation."""
        user, team_sub, _personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/billing/subscriptions/me/?context=")
        assert resp.status_code == 202
        mock_cancel.assert_called_once()
        passed_customer_id = mock_cancel.call_args.kwargs["stripe_customer_id"]
        assert passed_customer_id == team_sub.stripe_customer_id

    def test_delete_with_context_personal_skips_billing_authority(
        self, plan, plan_price, team_plan, team_plan_price
    ):
        """A concurrent-billing user who is NOT ``is_billing=True`` on the
        org can still cancel their personal sub via ``?context=personal``.
        The ``is_billing`` gate only applies to team-context mutations."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(
            email="other-owner@example.com",
            full_name="Other Owner",
        )
        org = Org.objects.create(name="OtherOrg", slug="other-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)

        # Plain non-billing member with their own personal sub
        member = User.objects.create_user(
            email="non-billing@example.com",
            full_name="Non Billing",
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        team_customer = StripeCustomer.objects.create(
            stripe_id="cus_other_team", org=org, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_other_team",
            stripe_customer=team_customer,
            status="active",
            plan=team_plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        personal_customer = StripeCustomer.objects.create(
            stripe_id="cus_member_personal", user=member, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_member_personal",
            stripe_customer=personal_customer,
            user=member,
            status="active",
            plan=plan,
            seat_limit=1,
            current_period_start=datetime(2025, 12, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 1, 1, tzinfo=UTC),
        )

        client = APIClient()
        client.force_authenticate(user=member)

        # Default context (team) is forbidden — no is_billing
        resp_team = client.delete("/api/v1/billing/subscriptions/me/")
        assert resp_team.status_code == 403

        # context=personal is allowed
        with (
            patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock),
            patch("apps.billing.views.send_subscription_cancel_notice_task"),
        ):
            resp_personal = client.delete("/api/v1/billing/subscriptions/me/?context=personal")
        assert resp_personal.status_code == 202

    def test_get_orders_team_before_personal(self, plan, plan_price, team_plan, team_plan_price):
        """``GET /me/`` orders the team sub first, then the personal sub —
        the contract callers rely on when displaying both side-by-side. The
        builder code appends team first, then personal, regardless of
        ``created_at`` (the personal sub here is intentionally older to
        prove the order is by context, not by timestamp)."""
        _user, team_sub, personal_sub = self._setup_concurrent(team_plan, plan)
        client = APIClient()
        client.force_authenticate(user=_user)

        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        assert resp.data["count"] == 2
        # Team sub was created later in the fixture, but the order in the
        # response is determined by the builder, which always appends team
        # first when both exist.
        assert str(resp.data["results"][0]["id"]) == str(team_sub.id)
        assert str(resp.data["results"][1]["id"]) == str(personal_sub.id)

    def test_get_dedupes_personal_sub_matching_both_indexes(
        self, plan, plan_price, team_plan, team_plan_price
    ):
        """Regression: ``_get_active_subscriptions_for_user`` queries the
        personal sub via two indexes (``Subscription.user_id`` and
        ``stripe_customer_id``). When the same row is found by both — the
        common case for personal subs created via the upgrade flow — it
        must appear exactly once in the response, not twice."""
        from apps.billing.models import Subscription

        user, _team_sub, personal_sub = self._setup_concurrent(team_plan, plan)

        # Sanity: confirm the personal sub is matched by BOTH queries —
        # ``user_id`` is set AND ``stripe_customer.user_id`` is set.
        assert personal_sub.user_id == user.id
        assert personal_sub.stripe_customer is not None
        assert personal_sub.stripe_customer.user_id == user.id

        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        # Two subs total (team + personal), with the personal sub appearing once.
        assert resp.data["count"] == 2
        personal_ids = [
            r["id"] for r in resp.data["results"] if str(r["id"]) == str(personal_sub.id)
        ]
        assert len(personal_ids) == 1
        # Confirm it's actually the same Subscription row from both indexes —
        # not two different rows that happen to share an id.
        assert (
            Subscription.objects.filter(user_id=user.id, stripe_customer__user_id=user.id).count()
            == 1
        )

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_patch_with_context_personal_refetches_personal_sub(
        self,
        mock_cancel,
        _mock_task,
        plan,
        plan_price,
        team_plan,
        team_plan_price,
    ):
        """``_refetch_subscription_after_mutation`` must return the personal
        sub (not whichever sub sorts newest) when ``context=personal`` is
        passed. The team sub here is intentionally newer (``2026-01`` start
        vs personal ``2025-12``) so a context-blind refetch would return
        the wrong row."""
        user, team_sub, personal_sub = self._setup_concurrent(team_plan, plan)
        # Make team sub strictly newer than personal sub by created_at as well.
        from apps.billing.models import Subscription

        Subscription.objects.filter(id=team_sub.id).update(
            created_at=personal_sub.created_at.replace(year=personal_sub.created_at.year + 1)
        )

        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.patch(
            "/api/v1/billing/subscriptions/me/?context=personal",
            {"cancel_at_period_end": True},
            format="json",
        )
        assert resp.status_code == 200
        # The response body is the *refetched* sub — must be the personal one,
        # not the team one (which is newer).
        assert str(resp.data["id"]) == str(personal_sub.id)
        # And the cancel call must have hit the personal customer.
        passed_customer_id = mock_cancel.call_args.kwargs["stripe_customer_id"]
        assert passed_customer_id == personal_sub.stripe_customer_id

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    def test_delete_returns_404_when_webhook_cancels_sub_mid_request(
        self, _mock_task, plan, plan_price, team_plan, team_plan_price
    ):
        """Race window: between the Stripe call and the post-mutation refetch,
        a ``customer.subscription.deleted`` webhook arrives and flips the row
        to ``canceled``. ``_refetch_subscription_after_mutation`` then finds
        no active subs and raises ``NotFound``. The Stripe-side cancel still
        happened — this test pins the (acceptable) 404 outcome of the refetch
        race so a future regression that swallows the NotFound or returns
        500 instead is caught."""
        from apps.billing.models import Subscription

        user, team_sub, _personal_sub = self._setup_concurrent(team_plan, plan)

        async def _flip_sub_to_canceled(**_kwargs: object) -> None:
            # Simulate the webhook winning the race: the team sub is gone by
            # the time we refetch, but the Stripe call already returned.
            await Subscription.objects.filter(id=team_sub.id).aupdate(status="canceled")

        client = APIClient()
        client.force_authenticate(user=user)

        with patch(
            "apps.billing.views.cancel_subscription",
            new=AsyncMock(side_effect=_flip_sub_to_canceled),
        ):
            resp = client.delete("/api/v1/billing/subscriptions/me/?context=team")
        assert resp.status_code == 404

    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_delete_with_context_team_returns_404_when_only_personal_exists(
        self, _mock_cancel, _mock_task, plan, plan_price, team_plan, team_plan_price
    ):
        """If a non-org-member user (no org membership) somehow passes
        ``?context=team``, ``_resolve_billing_customer`` cannot find a team
        customer and the request short-circuits to 404 — the
        ``_require_billing_authority`` gate also rejects it (403). Either
        outcome is acceptable; the important behavior is that the personal
        sub is never wrongly hit by a team-context mutation."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.users.models import User

        user = User.objects.create_user(
            email="personal-only@example.com",
            full_name="Personal Only",
        )
        personal_customer = StripeCustomer.objects.create(
            stripe_id="cus_personal_only", user=user, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_personal_only",
            stripe_customer=personal_customer,
            user=user,
            status="active",
            plan=plan,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        client = APIClient()
        client.force_authenticate(user=user)

        resp = client.delete("/api/v1/billing/subscriptions/me/?context=team")
        # Either 404 (no team customer) or 403 (no is_billing membership) —
        # both prove the personal sub is not silently mutated by a team
        # context request.
        assert resp.status_code in (403, 404)


@pytest.mark.django_db
class TestBillingAuthorityOnMutations:
    @patch("apps.billing.views.send_subscription_cancel_notice_task")
    @patch("apps.billing.views.cancel_subscription", new_callable=AsyncMock)
    def test_billing_member_can_delete(
        self, mock_cancel, _mock_task, org_member_client, team_org_setup
    ):
        resp = org_member_client.delete("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 202
        mock_cancel.assert_called_once()

    def test_non_billing_member_delete_returns_403(self, team_org_setup):
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import User

        org, _, _ = team_org_setup
        member = User.objects.create_user(
            email="nb-del@example.com",
            full_name="NB Del",
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
            {"plan_price_id": str(team_plan_price.id), "seat_limit": 3},
            format="json",
        )
        assert resp.status_code == 200
        mock_change.assert_called_once()

    def test_non_billing_member_patch_returns_403(self, team_org_setup, team_plan_price):
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import User

        org, _, _ = team_org_setup
        member = User.objects.create_user(
            email="nb-patch@example.com",
            full_name="NB Patch",
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(team_plan_price.id), "seat_limit": 3},
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
        from apps.users.models import User

        org, _, _ = team_org_setup
        extra_billing = User.objects.create_user(
            email="finance@example.com",
            full_name="Finance",
        )
        OrgMember.objects.create(org=org, user=extra_billing, role=OrgRole.MEMBER, is_billing=True)
        non_billing = User.objects.create_user(
            email="eng@example.com",
            full_name="Eng",
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


def _setup_org_member_client(role, *, is_billing: bool = False, label: str = "team"):
    """Create an org-member user + org + authed client for product-checkout
    tests. Each test rolls back its own transaction (``@pytest.mark.django_db``)
    so the email/slug only needs to be unique *within* a single test, not across
    the suite. ``label`` lets two classes that exercise the same role share one
    helper without colliding on the unique slug if both classes ever run inside
    the same transaction (defensive)."""
    from apps.orgs.models import Org, OrgMember
    from apps.users.models import User

    user = User.objects.create_user(
        email=f"{label}-{role.value}@example.com",
        full_name=f"{role.value} User",
    )
    org = Org.objects.create(
        name=f"{label.title()} Org",
        slug=f"{label}-org-{role.value}",
        created_by=user,
    )
    OrgMember.objects.create(org=org, user=user, role=role, is_billing=is_billing)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, org, client


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
        return _setup_org_member_client(role, is_billing=is_billing, label="team")

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


@pytest.mark.django_db
class TestProductCheckoutContextSelector:
    """``?context=personal|team`` selector for callers who can buy under both
    scopes (rule 5a/5b — e.g. an org-member owner who kept their personal sub
    after upgrade)."""

    def _setup_org(self, role, is_billing=False):
        return _setup_org_member_client(role, is_billing=is_billing, label="ctx")

    @patch("apps.billing.views.create_product_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_org_owner_personal_context_routes_to_user_customer(
        self, mock_customer, mock_session, boost_product, boost_product_price, mock_stripe_customer
    ):
        from apps.orgs.models import OrgRole

        user, _, client = self._setup_org(OrgRole.OWNER, is_billing=True)
        mock_customer.return_value = mock_stripe_customer
        mock_session.return_value = "https://checkout.stripe.com/personal"

        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/?context=personal",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        # No org_id in metadata — webhook will grant to the user balance.
        metadata = mock_session.call_args.kwargs["metadata"]
        assert metadata == {"product_id": str(boost_product.id)}
        # Customer resolution must use user_id, not org_id.
        assert mock_customer.call_args.kwargs.get("user_id") == user.id
        assert "org_id" not in mock_customer.call_args.kwargs

    @patch("apps.billing.views.create_product_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_org_admin_can_purchase_personal_context(
        self, mock_customer, mock_session, boost_product_price, mock_stripe_customer
    ):
        """Admins can't buy for the org, but ``?context=personal`` is anyone's
        right — the owner-only gate applies only when spending org funds."""
        from apps.orgs.models import OrgRole

        user, _, client = self._setup_org(OrgRole.ADMIN)
        mock_customer.return_value = mock_stripe_customer
        mock_session.return_value = "https://checkout.stripe.com/personal-admin"

        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/?context=personal",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 200
        assert mock_customer.call_args.kwargs.get("user_id") == user.id

    @patch("apps.billing.views.create_product_checkout_session", new_callable=AsyncMock)
    @patch("apps.billing.views.get_or_create_customer", new_callable=AsyncMock)
    def test_org_owner_explicit_team_context_routes_to_org(
        self, mock_customer, mock_session, boost_product, boost_product_price, mock_stripe_customer
    ):
        from apps.orgs.models import OrgRole

        _, org, client = self._setup_org(OrgRole.OWNER, is_billing=True)
        mock_customer.return_value = mock_stripe_customer
        mock_session.return_value = "https://checkout.stripe.com/team-explicit"

        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/?context=team",
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
        assert mock_customer.call_args.kwargs.get("org_id") == org.id

    def test_org_admin_explicit_team_context_returns_403(self, boost_product_price):
        from apps.orgs.models import OrgRole

        _, _, client = self._setup_org(OrgRole.ADMIN)
        resp = client.post(
            "/api/v1/billing/product-checkout-sessions/?context=team",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 403

    def test_personal_user_team_context_returns_400(self, authed_client, boost_product_price):
        resp = authed_client.post(
            "/api/v1/billing/product-checkout-sessions/?context=team",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "context" in resp.data

    def test_invalid_context_value_returns_400(self, authed_client, boost_product_price):
        resp = authed_client.post(
            "/api/v1/billing/product-checkout-sessions/?context=bogus",
            {
                "product_price_id": str(boost_product_price.id),
                "success_url": "https://localhost/ok",
                "cancel_url": "https://localhost/no",
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "context" in resp.data


# ---------------------------------------------------------------------------
# GET /api/v1/billing/credits/me/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreditBalanceView:
    def test_personal_user_gets_zero_by_default(self, authed_client, user):
        resp = authed_client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balances": [{"balance": 0, "scope": "user"}]}

    def test_personal_user_sees_own_balance(self, authed_client, user):
        from apps.billing.models import CreditBalance

        CreditBalance.objects.create(user=user, balance=125)
        resp = authed_client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balances": [{"balance": 125, "scope": "user"}]}

    def test_org_member_sees_org_balance(self, org_member_user, team_org_setup):
        from apps.billing.models import CreditBalance

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=500)
        client = APIClient()
        client.force_authenticate(user=org_member_user)
        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balances": [{"balance": 500, "scope": "org"}]}

    def test_org_member_with_leftover_personal_balance_sees_both(
        self, org_member_user, team_org_setup
    ):
        """Pre-upgrade personal credits remain visible after a personal→team
        upgrade (rule 16). The org balance is always emitted; the user balance
        is appended iff > 0."""
        from apps.billing.models import CreditBalance

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=500)
        CreditBalance.objects.create(user=org_member_user, balance=75)
        client = APIClient()
        client.force_authenticate(user=org_member_user)
        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {
            "balances": [
                {"balance": 500, "scope": "org"},
                {"balance": 75, "scope": "user"},
            ]
        }

    def test_org_member_with_zero_personal_balance_omits_user_entry(
        self, org_member_user, team_org_setup
    ):
        """A zero-valued personal CreditBalance row is treated the same as no
        row — we don't surface a noisy ``user`` entry for org members who
        never had pre-upgrade credits."""
        from apps.billing.models import CreditBalance

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=500)
        CreditBalance.objects.create(user=org_member_user, balance=0)
        client = APIClient()
        client.force_authenticate(user=org_member_user)
        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balances": [{"balance": 500, "scope": "org"}]}

    def test_non_billing_member_still_sees_org_balance(self, team_org_setup):
        """Read access to the org's credit balance is granted to any member,
        consistent with /subscriptions/me/ read semantics."""
        from apps.billing.models import CreditBalance
        from apps.orgs.models import OrgMember, OrgRole
        from apps.users.models import User

        org, _, _ = team_org_setup
        CreditBalance.objects.create(org=org, balance=42)
        member = User.objects.create_user(
            email="plain-credits@example.com",
            full_name="Plain",
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER, is_billing=False)
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.get("/api/v1/billing/credits/me/")
        assert resp.status_code == 200
        assert resp.data == {"balances": [{"balance": 42, "scope": "org"}]}


@pytest.mark.django_db
class TestPatchSubscriptionDeferredDowngrade:
    """The PATCH endpoint now passes ``new_price_amount`` to ``change_plan``
    so the service can route downgrades to a deferred SubscriptionSchedule.
    The defer logic itself is covered in core; here we only verify the view
    forwards the amount correctly."""

    @patch("apps.billing.views.change_plan", new_callable=AsyncMock)
    def test_patch_forwards_new_price_amount_to_change_plan(
        self, mock_change, authed_client, subscription, plan_price
    ):
        resp = authed_client.patch(
            "/api/v1/billing/subscriptions/me/",
            {"plan_price_id": str(plan_price.id)},
            format="json",
        )
        assert resp.status_code == 200
        # plan_price fixture is amount=999. Without this kwarg, change_plan
        # falls back to its legacy immediate-modify path and downgrades would
        # never defer.
        assert mock_change.call_args.kwargs["new_price_amount"] == 999


@pytest.mark.django_db
class TestSubscriptionSerializerExposesSchedule:
    """``GET /billing/subscriptions/me/`` exposes the scheduled-change mirror
    so the frontend can render the "downgrading on <date>" badge."""

    def test_no_schedule_returns_null_fields(self, authed_client, subscription):
        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        result = resp.data["results"][0]
        assert result["scheduled_plan"] is None
        assert result["scheduled_change_at"] is None

    def test_schedule_set_returns_target_plan_and_timestamp(
        self, authed_client, subscription, team_plan
    ):
        """When ``scheduled_plan`` + ``scheduled_change_at`` are populated
        (mirror written by the schedule webhook), the serializer surfaces
        the nested target plan and the ISO timestamp."""
        from apps.billing.models import Subscription

        Subscription.objects.filter(id=subscription.id).update(
            scheduled_plan=team_plan,
            scheduled_change_at=datetime(2026, 6, 1, tzinfo=UTC),
        )

        resp = authed_client.get("/api/v1/billing/subscriptions/me/")
        assert resp.status_code == 200
        result = resp.data["results"][0]
        assert result["scheduled_plan"]["id"] == str(team_plan.id)
        assert result["scheduled_change_at"].startswith("2026-06-01")


@pytest.mark.django_db
class TestScheduledChangeView:
    """``DELETE /billing/subscriptions/me/scheduled-change/`` releases an
    active SubscriptionSchedule so the user keeps their current plan."""

    def test_delete_calls_release_for_personal_context(
        self, authed_client, subscription
    ):
        """Default routing: a non-org-member user hits the personal path,
        and the view delegates to ``release_pending_schedule_for_customer``."""
        # The view does a lazy import — patch the symbol on the source module.
        with patch(
            "saasmint_core.services.billing.release_pending_schedule_for_customer",
            new_callable=AsyncMock,
        ) as mock_release:
            resp = authed_client.delete(
                "/api/v1/billing/subscriptions/me/scheduled-change/"
            )
        assert resp.status_code == 200
        mock_release.assert_called_once()

    def test_delete_404_when_no_active_subscription(self, authed_client):
        """No customer / no sub → 404, no Stripe call attempted."""
        resp = authed_client.delete(
            "/api/v1/billing/subscriptions/me/scheduled-change/"
        )
        assert resp.status_code == 404

    def test_delete_team_context_403_for_non_billing_member(self, team_plan_price):
        """Same is_billing gate as the other team-context mutations: a
        non-billing member cannot release the team sub's schedule."""
        from apps.billing.models import StripeCustomer, Subscription
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        member = User.objects.create_user(
            email="member-rel@example.com", full_name="Plain Member"
        )
        org = Org.objects.create(name="RelOrg", slug="rel-org", created_by=member)
        OrgMember.objects.create(
            org=org, user=member, role=OrgRole.MEMBER, is_billing=False
        )
        customer = StripeCustomer.objects.create(
            stripe_id="cus_rel_team", org=org, livemode=False
        )
        Subscription.objects.create(
            stripe_id="sub_rel_team",
            stripe_customer=customer,
            status="active",
            plan=team_plan_price.plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )
        client = APIClient()
        client.force_authenticate(user=member)

        resp = client.delete(
            "/api/v1/billing/subscriptions/me/scheduled-change/?context=team"
        )
        assert resp.status_code == 403
