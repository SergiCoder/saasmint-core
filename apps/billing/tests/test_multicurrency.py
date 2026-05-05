"""Tests focused on the multi-currency billing feature.

Covers behavior that's specific to non-USD billing — single-currency upsert
mechanics live in test_sync_stripe_catalog.py. The split keeps each suite's
mock setup small and the assertions specific to a single concern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.management import call_command

from apps.billing.models import (
    LocalizedPrice,
    Plan,
    PlanPrice,
    PlanTier,
    Product,
    Subscription,
)

pytestmark = pytest.mark.django_db


# ── /billing/currencies endpoint ──────────────────────────────────────────────


class TestBillingCurrenciesEndpoint:
    def test_returns_billable_and_display_only(self, settings, authed_client):
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        resp = authed_client.get("/api/v1/billing/currencies/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["billable"] == ["eur", "usd"]
        # Display-only is everything in SUPPORTED_CURRENCIES \ billable.
        assert "gbp" in body["display_only"]
        assert "usd" not in body["display_only"]

    def test_no_auth_required(self, client):
        resp = client.get("/api/v1/billing/currencies/")
        assert resp.status_code == 200


# ── sync_localized_prices stability gate ──────────────────────────────────────


def _fx_response(rates: dict[str, float]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "result": "success",
        "rates": {k.upper(): v for k, v in rates.items()},
    }
    return resp


class TestStabilityGate:
    def test_preserves_stripe_price_id_when_amount_unchanged(self, plan_price):
        # Seed an existing localized row with a Stripe Price ID that came
        # from a previous sync_stripe_catalog run.
        existing = LocalizedPrice.objects.create(
            plan_price=plan_price,
            currency="eur",
            amount_minor=899,
            stripe_price_id="price_eur_minted",
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Re-run with the same FX rate that produces the same friendly-rounded
        # value. amount=999 USD cents * 0.9 = 8.99 → 899 EUR cents.
        with patch(
            "apps.billing.tasks.httpx.get",
            return_value=_fx_response({"eur": 0.9, "gbp": 0.8, "jpy": 150, "cny": 7}),
        ):
            call_command("sync_localized_prices", stdout=StringIO())

        existing.refresh_from_db()
        # amount_minor unchanged AND stripe_price_id preserved.
        assert existing.amount_minor == 899
        assert existing.stripe_price_id == "price_eur_minted"

    def test_updates_amount_when_friendly_value_moves(self, plan_price):
        existing = LocalizedPrice.objects.create(
            plan_price=plan_price,
            currency="eur",
            amount_minor=899,
            stripe_price_id="price_eur_old",
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # FX shifts so 9.99 * rate now snaps to a different charm price.
        # 999 cents * 1.1 = 10.989 → round_friendly snaps to a different bucket.
        with patch(
            "apps.billing.tasks.httpx.get",
            return_value=_fx_response({"eur": 1.1, "gbp": 0.8, "jpy": 150, "cny": 7}),
        ):
            call_command("sync_localized_prices", stdout=StringIO())

        existing.refresh_from_db()
        # amount changed, but stripe_price_id is preserved (we never touch it
        # in sync_localized_prices — sync_stripe_catalog re-mints next run).
        assert existing.amount_minor != 899
        assert existing.stripe_price_id == "price_eur_old"


# ── sync_stripe_catalog: per-currency Stripe Price minting ────────────────────


def _empty_price_list() -> MagicMock:
    return MagicMock(data=[])


def _stripe_price_create_side_effect(call_index: list[int]) -> object:
    """Return a unique price ID per call so the partial-unique constraint is
    satisfied across multiple currency mints."""

    def _create(**_kwargs: object) -> MagicMock:
        call_index[0] += 1
        return MagicMock(id=f"price_minted_{call_index[0]}")

    return _create


class TestMultiCurrencySync:
    @pytest.fixture
    def plan_with_price(self):
        plan = Plan.objects.create(
            name="Solo Monthly",
            description="solo",
            context="personal",
            tier=PlanTier.BASIC,
            interval="month",
            is_active=True,
        )
        # No conftest fixtures — this test writes its own catalog so we can
        # assert exact Stripe call counts without seed-data noise.
        Product.objects.all().delete()
        Plan.objects.exclude(id=plan.id).delete()
        price = PlanPrice.objects.create(
            plan=plan, stripe_price_id="price_old_local", amount=1900
        )
        return plan, price

    def test_mints_one_stripe_price_per_billing_currency(self, plan_with_price, settings):
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        _, price = plan_with_price
        # Pre-seed the EUR LocalizedPrice row so the sync doesn't have to
        # bootstrap via the FX feed (we don't want to mock httpx here).
        LocalizedPrice.objects.create(
            plan_price=price,
            currency="eur",
            amount_minor=1700,
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        call_index = [0]
        with (
            patch("stripe.Price.list", return_value=_empty_price_list()),
            patch("stripe.Product.create", return_value=MagicMock(id="prod_x")),
            patch(
                "stripe.Price.create",
                side_effect=_stripe_price_create_side_effect(call_index),
            ) as mock_create,
            patch("stripe.Product.modify"),
            patch("stripe.Price.modify"),
        ):
            call_command("sync_stripe_catalog", stdout=StringIO(), stderr=StringIO())

        # USD + EUR = 2 mints for the single plan.
        assert mock_create.call_count == 2
        currencies = [c.kwargs["currency"] for c in mock_create.call_args_list]
        assert sorted(currencies) == ["eur", "usd"]

    def test_eur_stripe_price_id_lands_on_localized_price(self, plan_with_price, settings):
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        _, price = plan_with_price
        LocalizedPrice.objects.create(
            plan_price=price,
            currency="eur",
            amount_minor=1700,
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        # Stable IDs by currency: differentiate USD vs EUR.
        def _create(**kwargs: object) -> MagicMock:
            return MagicMock(id=f"price_minted_{kwargs['currency']}")

        with (
            patch("stripe.Price.list", return_value=_empty_price_list()),
            patch("stripe.Product.create", return_value=MagicMock(id="prod_x")),
            patch("stripe.Price.create", side_effect=_create),
            patch("stripe.Product.modify"),
            patch("stripe.Price.modify"),
        ):
            call_command("sync_stripe_catalog", stdout=StringIO(), stderr=StringIO())

        # USD lands on PlanPrice.stripe_price_id (existing column).
        price.refresh_from_db()
        assert price.stripe_price_id == "price_minted_usd"
        # EUR lands on LocalizedPrice.stripe_price_id (new column).
        eur = LocalizedPrice.objects.get(plan_price=price, currency="eur")
        assert eur.stripe_price_id == "price_minted_eur"


# ── checkout currency routing ─────────────────────────────────────────────────


class TestCheckoutCurrencyRouting:
    def test_eur_user_gets_eur_stripe_price_at_checkout(
        self, authed_client, plan_price, user
    ):
        eur_row = LocalizedPrice.objects.create(
            plan_price=plan_price,
            currency="eur",
            amount_minor=899,
            stripe_price_id="price_eur_billable",
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        user.preferred_currency = "eur"
        user.save()

        with (
            patch(
                "apps.billing.views.get_or_create_customer",
                new_callable=AsyncMock,
            ) as mock_cust,
            patch(
                "apps.billing.views.create_checkout_session",
                new_callable=AsyncMock,
            ) as mock_session,
        ):
            mock_cust.return_value = MagicMock(stripe_id="cus_test_eur")
            mock_session.return_value = "https://checkout.stripe.com/x"
            resp = authed_client.post(
                "/api/v1/billing/checkout-sessions/",
                {
                    "plan_price_id": str(plan_price.id),
                    "success_url": "https://localhost:3000/ok",
                    "cancel_url": "https://localhost:3000/cancel",
                },
                format="json",
            )

        assert resp.status_code == 200
        kwargs = mock_session.call_args.kwargs
        assert kwargs["price_id"] == eur_row.stripe_price_id
        assert kwargs["billing_currency"] == "eur"

    def test_falls_back_to_usd_when_currency_not_billable(
        self, authed_client, plan_price, user, settings
    ):
        # SEK is not in BILLING_CURRENCIES → fall back to USD silently.
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        user.preferred_currency = "sek"
        user.save()

        with (
            patch(
                "apps.billing.views.get_or_create_customer",
                new_callable=AsyncMock,
            ) as mock_cust,
            patch(
                "apps.billing.views.create_checkout_session",
                new_callable=AsyncMock,
            ) as mock_session,
        ):
            mock_cust.return_value = MagicMock(stripe_id="cus_test_sek")
            mock_session.return_value = "https://checkout.stripe.com/x"
            authed_client.post(
                "/api/v1/billing/checkout-sessions/",
                {
                    "plan_price_id": str(plan_price.id),
                    "success_url": "https://localhost:3000/ok",
                    "cancel_url": "https://localhost:3000/cancel",
                },
                format="json",
            )

        kwargs = mock_session.call_args.kwargs
        assert kwargs["billing_currency"] == "usd"
        assert kwargs["price_id"] == plan_price.stripe_price_id  # USD column


# ── checkout session params: automatic_tax + adaptive_pricing ────────────────


def _run_create_checkout(billing_currency: str) -> MagicMock:
    """Helper: invoke the async create_checkout_session and return the
    Stripe ``checkout.Session.create`` mock so the caller can assert on its
    kwargs. Mirrors the ``async_to_sync`` pattern used elsewhere in the
    billing tests."""
    from asgiref.sync import async_to_sync
    from saasmint_core.services.billing import create_checkout_session

    with patch(
        "saasmint_core.services.billing.stripe.checkout.Session.create",
        return_value=MagicMock(url="https://x"),
    ) as mock_create:
        async_to_sync(create_checkout_session)(
            stripe_customer_id="cus_x",
            price_id="price_x",
            client_reference_id="user_x",
            billing_currency=billing_currency,
            success_url="https://ok",
            cancel_url="https://cancel",
        )
    return mock_create


class TestCheckoutSessionParams:
    def test_automatic_tax_always_enabled(self):
        mock_create = _run_create_checkout("eur")
        assert mock_create.call_args.kwargs["automatic_tax"] == {"enabled": True}

    def test_adaptive_pricing_disabled_for_non_usd(self):
        mock_create = _run_create_checkout("eur")
        assert mock_create.call_args.kwargs["adaptive_pricing"] == {"enabled": False}

    def test_adaptive_pricing_enabled_for_usd(self):
        mock_create = _run_create_checkout("usd")
        assert mock_create.call_args.kwargs["adaptive_pricing"] == {"enabled": True}


# ── PATCH /subscriptions/me/: cross-currency rejection ────────────────────────


class TestSubscriptionPatchCurrency:
    def test_rejects_cross_currency_plan_change(
        self, authed_client, subscription, plan_price
    ):
        # Existing sub is in EUR; user tries to switch to a plan whose only
        # billable currency is USD (no EUR LocalizedPrice with stripe_price_id).
        subscription.currency = "eur"
        subscription.save()

        # New target plan with no EUR Stripe Price minted.
        new_plan = Plan.objects.create(
            name="Pro Monthly",
            context="personal",
            tier=PlanTier.PRO,
            interval="month",
            is_active=True,
        )
        new_plan_price = PlanPrice.objects.create(
            plan=new_plan, stripe_price_id="price_pro_usd", amount=4999
        )

        with patch(
            "apps.billing.views._get_customer_and_paid_subscription",
            new_callable=AsyncMock,
        ) as mock_get:
            from saasmint_core.domain.subscription import (
                Subscription as DomainSubscription,
            )
            from saasmint_core.domain.subscription import (
                SubscriptionStatus,
            )

            domain_sub = DomainSubscription(
                id=subscription.id,
                stripe_id=subscription.stripe_id,
                stripe_customer_id=subscription.stripe_customer_id,
                user_id=None,
                status=SubscriptionStatus.ACTIVE,
                plan_id=subscription.plan_id,
                seat_limit=1,
                current_period_start=subscription.current_period_start,
                current_period_end=subscription.current_period_end,
                currency="eur",
                created_at=subscription.created_at,
            )
            mock_get.return_value = (
                MagicMock(id=subscription.stripe_customer_id),
                domain_sub,
                subscription.stripe_id,
            )
            resp = authed_client.patch(
                "/api/v1/billing/subscriptions/me/",
                {"plan_price_id": str(new_plan_price.id)},
                format="json",
            )

        assert resp.status_code == 400
        body = resp.json()
        # DRF validation errors arrive in the standard envelope; field name is
        # "plan_price_id" per the helper's ValidationError.
        assert "plan_price_id" in str(body)


# ── webhook persists subscription.currency ────────────────────────────────────


class TestWebhookCurrency:
    def test_sync_subscription_persists_currency(
        self, plan, plan_price, stripe_customer
    ):
        from asgiref.sync import async_to_sync
        from saasmint_core.services.webhooks import sync_subscription_from_data

        from apps.billing.repositories import get_webhook_repos

        repos = get_webhook_repos()
        sub_data = {
            "id": "sub_test_eur_xyz",
            "customer": stripe_customer.stripe_id,
            "status": "active",
            "currency": "eur",  # Stripe sends this top-level
            "items": {
                "data": [
                    {
                        "price": {
                            "id": plan_price.stripe_price_id,
                            "currency": "eur",
                        },
                        "current_period_start": 1735689600,  # 2025-01-01
                        "current_period_end": 1738368000,  # 2025-02-01
                        "quantity": 1,
                    }
                ],
            },
            "trial_end": None,
            "canceled_at": None,
            "cancel_at": None,
        }

        async_to_sync(sync_subscription_from_data)(
            sub_data,
            customers=repos.customers,
            subscriptions=repos.subscriptions,
            plans=repos.plans,
        )

        sub = Subscription.objects.get(stripe_id="sub_test_eur_xyz")
        assert sub.currency == "eur"


# ── catalog dual-display rendering ────────────────────────────────────────────


class TestDualDisplay:
    def test_local_fields_null_when_preferred_billable(
        self, authed_client, plan_price, user, settings
    ):
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        # Localize EUR for display correctness on the primary line.
        LocalizedPrice.objects.create(
            plan_price=plan_price,
            currency="eur",
            amount_minor=899,
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        user.preferred_currency = "eur"
        user.save()

        resp = authed_client.get("/api/v1/billing/plans/")
        result = resp.json()["results"][0]["price"]
        # Preferred currency (EUR) is billable → no fallback → no dual display.
        assert result["currency"] == "eur"
        assert result["local_display_amount"] is None
        assert result["local_currency"] is None

    def test_local_fields_populated_when_preferred_non_billable(
        self, authed_client, plan_price, user, settings
    ):
        settings.BILLING_CURRENCIES = ["usd", "eur"]
        # SEK is display-only; user prefers it.
        LocalizedPrice.objects.create(
            plan_price=plan_price,
            currency="sek",
            amount_minor=10999,  # 109.99 SEK
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        user.preferred_currency = "sek"
        user.save()

        resp = authed_client.get("/api/v1/billing/plans/")
        result = resp.json()["results"][0]["price"]
        # Charge: USD fallback (SEK not billable). Display: SEK approx alongside.
        assert result["currency"] == "usd"
        assert result["display_amount"] == 9.99
        assert result["local_currency"] == "sek"
        assert result["local_display_amount"] == 109.99

    def test_local_fields_null_for_unauthenticated_request(self, client, plan_price):
        resp = client.get("/api/v1/billing/plans/")
        result = resp.json()["results"][0]["price"]
        # Anon user → no preferred currency → no dual display.
        assert result["local_display_amount"] is None
        assert result["local_currency"] is None
