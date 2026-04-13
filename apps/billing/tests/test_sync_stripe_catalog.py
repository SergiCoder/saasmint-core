"""Tests for the ``sync_stripe_catalog`` management command.

Stripe is fully mocked — no network calls. The command's job is to
upsert Stripe Products/Prices via lookup keys and write the resulting
price IDs back to local PlanPrice / ProductPrice rows.
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.management import call_command

from apps.billing.management.commands.sync_stripe_catalog import (
    _plan_lookup_key,
    _product_lookup_key,
    _slug,
)
from apps.billing.models import Plan, PlanPrice, Product, ProductPrice

pytestmark = pytest.mark.django_db


# ── helpers ───────────────────────────────────────────────────────────────────


def _empty_price_list() -> MagicMock:
    """A ``stripe.Price.list`` response containing no matching prices."""
    return MagicMock(data=[])


def _existing_price(
    *,
    price_id: str = "price_existing",
    unit_amount: int = 1900,
    currency: str = "usd",
    interval: str | None = "month",
    product_id: str = "prod_existing",
    product_name: str = "Existing",
    product_description: str | None = None,
    product_metadata: dict[str, str] | None = None,
) -> MagicMock:
    """Build a fake stripe.Price object exposing the attributes the command reads."""
    price = MagicMock(spec=stripe.Price)
    price.id = price_id
    price.unit_amount = unit_amount
    price.currency = currency
    price.recurring = SimpleNamespace(interval=interval) if interval is not None else None

    product = MagicMock(spec=stripe.Product)
    product.id = product_id
    product.name = product_name
    product.description = product_description
    metadata_obj = MagicMock()
    metadata_obj.to_dict.return_value = product_metadata or {}
    product.metadata = metadata_obj
    price.product = product
    return price


def _run() -> str:
    """Invoke the command and return its stdout."""
    out = StringIO()
    call_command("sync_stripe_catalog", stdout=out, stderr=StringIO())
    return out.getvalue()


@pytest.fixture(autouse=True)
def _clear_seeded_catalog():
    """Migrations seed Boost products + price rows. Clear them so each test
    starts from a known-empty catalog and assertions about Stripe call counts
    aren't polluted by the seed data."""
    Product.objects.all().delete()
    Plan.objects.all().delete()
    yield


# ── pure helpers ──────────────────────────────────────────────────────────────


class TestLookupKeys:
    def test_slug_handles_punctuation(self):
        assert _slug("100 Credits!") == "100_credits"
        assert _slug("Pack — Plus") == "pack_plus"
        assert _slug("ABC___xyz") == "abc_xyz"

    def test_plan_lookup_key(self):
        plan = Plan.objects.create(
            name="Personal Pro Monthly",
            context="personal",
            tier=3,
            interval="month",
            is_active=True,
        )
        assert _plan_lookup_key(plan) == "plan_personal_pro_month"

    def test_product_lookup_key(self):
        product = Product.objects.create(
            name="100 Credits", type="one_time", credits=100, is_active=True
        )
        assert _product_lookup_key(product) == "product_100_credits"


# ── early-exit when stripe key is missing ─────────────────────────────────────


class TestStripeKeyGuard:
    def test_aborts_when_stripe_api_key_missing(self):
        out, err = StringIO(), StringIO()
        with patch("stripe.api_key", ""):
            call_command("sync_stripe_catalog", stdout=out, stderr=err)
        assert "STRIPE_SECRET_KEY is not configured" in err.getvalue()
        # No success line emitted because we aborted early.
        assert "complete" not in out.getvalue()


# ── plan sync ─────────────────────────────────────────────────────────────────


@pytest.fixture
def paid_plan_with_price():
    plan = Plan.objects.create(
        name="Personal Basic Monthly",
        description="Basic monthly",
        context="personal",
        tier=2,
        interval="month",
        is_active=True,
    )
    price = PlanPrice.objects.create(plan=plan, stripe_price_id="price_old_local", amount=1900)
    return plan, price


class TestSyncPlans:
    def test_creates_new_stripe_product_and_price_when_none_exists(self, paid_plan_with_price):
        plan, price = paid_plan_with_price

        new_price = MagicMock(id="price_new_stripe")
        new_product = MagicMock(id="prod_new_stripe")

        with (
            patch("stripe.Price.list", return_value=_empty_price_list()) as mock_list,
            patch("stripe.Product.create", return_value=new_product) as mock_pcreate,
            patch("stripe.Price.create", return_value=new_price) as mock_pricecreate,
            patch("stripe.Product.modify") as mock_pmodify,
            patch("stripe.Price.modify") as mock_price_modify,
        ):
            _run()

        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs["lookup_keys"] == [_plan_lookup_key(plan)]
        mock_pcreate.assert_called_once()
        # Created Product carries plan name, description, kind metadata
        kwargs = mock_pcreate.call_args.kwargs
        assert kwargs["name"] == plan.name
        assert kwargs["description"] == "Basic monthly"
        assert kwargs["metadata"]["local_plan_id"] == str(plan.id)
        assert kwargs["metadata"]["kind"] == "plan"

        # Price.create called with recurring and lookup_key transfer
        mock_pricecreate.assert_called_once()
        price_kwargs = mock_pricecreate.call_args.kwargs
        assert price_kwargs["product"] == "prod_new_stripe"
        assert price_kwargs["unit_amount"] == 1900
        assert price_kwargs["currency"] == "usd"
        assert price_kwargs["recurring"] == {"interval": "month"}
        assert price_kwargs["lookup_key"] == _plan_lookup_key(plan)
        assert price_kwargs["transfer_lookup_key"] is True

        mock_pmodify.assert_not_called()
        mock_price_modify.assert_not_called()

        price.refresh_from_db()
        assert price.stripe_price_id == "price_new_stripe"

    def test_existing_price_in_sync_is_no_op(self, paid_plan_with_price):
        plan, price = paid_plan_with_price
        existing = _existing_price(
            price_id="price_already_synced",
            unit_amount=1900,
            interval="month",
            product_name=plan.name,
            product_description="Basic monthly",
            product_metadata={"local_plan_id": str(plan.id), "kind": "plan"},
        )
        # Mark the local row as already pointing at the existing Stripe price
        price.stripe_price_id = "price_already_synced"
        price.save()

        list_resp = MagicMock(data=[existing])
        with (
            patch("stripe.Price.list", return_value=list_resp),
            patch("stripe.Price.create") as mock_create,
            patch("stripe.Price.modify") as mock_modify,
            patch("stripe.Product.create") as mock_pcreate,
            patch("stripe.Product.modify") as mock_pmodify,
        ):
            out = _run()

        mock_create.assert_not_called()
        mock_modify.assert_not_called()
        mock_pcreate.assert_not_called()
        # Product metadata already matches → no modify either
        mock_pmodify.assert_not_called()
        assert "already in sync" in out
        price.refresh_from_db()
        assert price.stripe_price_id == "price_already_synced"

    def test_amount_drift_archives_old_price_and_creates_new(self, paid_plan_with_price):
        plan, price = paid_plan_with_price
        existing = _existing_price(
            price_id="price_stale",
            unit_amount=999,  # local says 1900 → mismatch
            interval="month",
            product_id="prod_keep",
            product_metadata={"local_plan_id": str(plan.id), "kind": "plan"},
        )
        list_resp = MagicMock(data=[existing])
        new_price = MagicMock(id="price_new_after_drift")

        with (
            patch("stripe.Price.list", return_value=list_resp),
            patch("stripe.Price.modify") as mock_archive,
            patch("stripe.Price.create", return_value=new_price) as mock_create,
            patch("stripe.Product.create") as mock_pcreate,
            patch("stripe.Product.modify"),
        ):
            _run()

        # Old price archived (active=False), product reused (no Product.create)
        mock_archive.assert_called_once_with("price_stale", active=False)
        mock_pcreate.assert_not_called()
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["product"] == "prod_keep"
        assert mock_create.call_args.kwargs["unit_amount"] == 1900
        price.refresh_from_db()
        assert price.stripe_price_id == "price_new_after_drift"

    def test_existing_product_metadata_drift_triggers_modify(self, paid_plan_with_price):
        plan, _price = paid_plan_with_price
        # Existing matches amount/interval but product metadata is empty
        existing = _existing_price(
            price_id="price_meta_synced",
            unit_amount=1900,
            interval="month",
            product_metadata={},  # missing local_plan_id, kind
        )
        list_resp = MagicMock(data=[existing])
        with (
            patch("stripe.Price.list", return_value=list_resp),
            patch("stripe.Product.modify") as mock_pmodify,
            patch("stripe.Price.create") as mock_create,
            patch("stripe.Price.modify") as mock_price_modify,
        ):
            _run()

        mock_create.assert_not_called()
        mock_price_modify.assert_not_called()
        mock_pmodify.assert_called_once()
        update = mock_pmodify.call_args.kwargs
        assert update["metadata"]["local_plan_id"] == str(plan.id)
        assert update["metadata"]["kind"] == "plan"

    def test_skips_plan_without_price_row(self):
        Plan.objects.create(
            name="Orphan",
            context="personal",
            tier=2,
            interval="month",
            is_active=True,
        )
        with (
            patch("stripe.Price.list") as mock_list,
            patch("stripe.Price.create") as mock_create,
            patch("stripe.Product.create") as mock_pcreate,
        ):
            out = _run()
        mock_list.assert_not_called()
        mock_create.assert_not_called()
        mock_pcreate.assert_not_called()
        assert "no PlanPrice row" in out

    def test_skips_free_plan_with_zero_amount(self):
        plan = Plan.objects.create(
            name="Personal Free",
            context="personal",
            tier=1,
            interval="month",
            is_active=True,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="local_free", amount=0)
        with (
            patch("stripe.Price.list") as mock_list,
            patch("stripe.Price.create") as mock_create,
            patch("stripe.Product.create") as mock_pcreate,
        ):
            out = _run()
        mock_list.assert_not_called()
        mock_create.assert_not_called()
        mock_pcreate.assert_not_called()
        assert "free plan" in out.lower()

    def test_inactive_plan_skipped(self):
        plan = Plan.objects.create(
            name="Retired",
            context="personal",
            tier=2,
            interval="month",
            is_active=False,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_x", amount=999)
        with (
            patch("stripe.Price.list") as mock_list,
            patch("stripe.Price.create") as mock_create,
        ):
            _run()
        mock_list.assert_not_called()
        mock_create.assert_not_called()


# ── product sync ──────────────────────────────────────────────────────────────


@pytest.fixture
def product_with_price():
    product = Product.objects.create(
        name="100 Credits", type="one_time", credits=100, is_active=True
    )
    price = ProductPrice.objects.create(
        product=product, stripe_price_id="price_old_prod_local", amount=999
    )
    return product, price


class TestSyncProducts:
    def test_creates_new_one_time_price(self, product_with_price):
        product, price = product_with_price
        new_price = MagicMock(id="price_new_credits")
        new_product = MagicMock(id="prod_new_credits")
        with (
            patch("stripe.Price.list", return_value=_empty_price_list()),
            patch("stripe.Product.create", return_value=new_product),
            patch("stripe.Price.create", return_value=new_price) as mock_create,
            patch("stripe.Price.modify"),
            patch("stripe.Product.modify"),
        ):
            _run()

        kwargs = mock_create.call_args.kwargs
        # Product (one-time) prices have no recurring
        assert "recurring" not in kwargs
        assert kwargs["unit_amount"] == 999
        assert kwargs["lookup_key"] == _product_lookup_key(product)
        price.refresh_from_db()
        assert price.stripe_price_id == "price_new_credits"

    def test_recurring_drift_from_one_time_to_recurring_archives(self, product_with_price):
        """Existing price has recurring set, but local product is one-time → mismatch."""
        _, price = product_with_price
        existing = _existing_price(
            price_id="price_was_recurring",
            unit_amount=999,
            interval="month",  # local is None → mismatch
        )
        list_resp = MagicMock(data=[existing])
        new_price = MagicMock(id="price_now_one_time")
        with (
            patch("stripe.Price.list", return_value=list_resp),
            patch("stripe.Price.modify") as mock_archive,
            patch("stripe.Price.create", return_value=new_price),
            patch("stripe.Product.create"),
            patch("stripe.Product.modify"),
        ):
            _run()
        mock_archive.assert_called_once_with("price_was_recurring", active=False)
        price.refresh_from_db()
        assert price.stripe_price_id == "price_now_one_time"

    def test_currency_drift_archives_and_recreates(self, product_with_price):
        _, _price = product_with_price
        existing = _existing_price(
            price_id="price_eur",
            unit_amount=999,
            currency="eur",  # local hardcodes usd → mismatch
            interval=None,
        )
        list_resp = MagicMock(data=[existing])
        new_price = MagicMock(id="price_usd")
        with (
            patch("stripe.Price.list", return_value=list_resp),
            patch("stripe.Price.modify") as mock_archive,
            patch("stripe.Price.create", return_value=new_price),
            patch("stripe.Product.create"),
            patch("stripe.Product.modify"),
        ):
            _run()
        mock_archive.assert_called_once_with("price_eur", active=False)

    def test_inactive_product_skipped(self):
        product = Product.objects.create(
            name="Old Pack", type="one_time", credits=10, is_active=False
        )
        ProductPrice.objects.create(product=product, stripe_price_id="price_old", amount=99)
        with (
            patch("stripe.Price.list") as mock_list,
            patch("stripe.Price.create") as mock_create,
        ):
            _run()
        mock_list.assert_not_called()
        mock_create.assert_not_called()

    def test_skips_product_without_price_row(self):
        Product.objects.create(name="No Price Yet", type="one_time", credits=50, is_active=True)
        with (
            patch("stripe.Price.list") as mock_list,
            patch("stripe.Price.create") as mock_create,
        ):
            out = _run()
        mock_list.assert_not_called()
        mock_create.assert_not_called()
        assert "no ProductPrice row" in out
