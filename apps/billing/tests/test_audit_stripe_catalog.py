"""Tests for the audit_stripe_catalog management command.

The command is read-only by default (lists stray Stripe products); --archive
sets ``active=False`` on each stray product that has no active subscription.
All Stripe API calls are patched so the test suite never touches the network.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command


def _make_stripe_product(
    *,
    product_id: str,
    name: str = "Test Product",
    metadata: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like a ``stripe.Product``."""
    m = MagicMock()
    m.id = product_id
    m.name = name
    if metadata:
        m.metadata.to_dict.return_value = metadata
    else:
        m.metadata = None
    return m


def _make_price(*, price_id: str, product_id: str, recurring: object = True) -> MagicMock:
    """Build a MagicMock that quacks like a ``stripe.Price``."""
    p = MagicMock()
    p.id = price_id
    p.product = product_id
    p.recurring = recurring  # None for one-time prices
    return p


@pytest.mark.django_db
class TestAuditStripeCatalogCommand:
    def test_no_stripe_key_prints_error(self, settings):
        """When ``STRIPE_SECRET_KEY`` is absent, the command must print an
        error to stderr and exit early without calling ``Product.list``."""
        settings.STRIPE_SECRET_KEY = ""

        out = StringIO()
        err = StringIO()
        with patch("stripe.api_key", ""):
            call_command("audit_stripe_catalog", stdout=out, stderr=err)

        assert "STRIPE_SECRET_KEY is not configured" in err.getvalue()

    def test_all_products_owned_prints_zero_strays(self, db):
        """Every active Stripe product maps to a local Plan → no strays."""
        from apps.billing.models import Plan

        plan = Plan.objects.create(
            name="Pro Monthly", context="personal", interval="month", is_active=True
        )
        stripe_product = _make_stripe_product(
            product_id="prod_local",
            metadata={"kind": "plan", "local_plan_id": str(plan.id)},
        )

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stripe_product])),
            ),
        ):
            call_command("audit_stripe_catalog", stdout=out, stderr=StringIO())

        output = out.getvalue()
        assert "Owned by local catalog: 1" in output
        assert "Stray (no matching local row): 0" in output

    def test_unknown_product_listed_as_stray(self, db):
        """A Stripe product whose metadata doesn't match any local row is
        listed as stray."""
        stray = _make_stripe_product(
            product_id="prod_orphan",
            name="Old Experiment",
            metadata={"kind": "plan", "local_plan_id": "00000000-0000-0000-0000-000000000000"},
        )

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stray])),
            ),
        ):
            call_command("audit_stripe_catalog", stdout=out, stderr=StringIO())

        output = out.getvalue()
        assert "Stray (no matching local row): 1" in output
        assert "prod_orphan" in output
        # Without --archive, a reminder is printed.
        assert "--archive" in output

    def test_stray_product_archived_when_no_active_sub(self, db):
        """With ``--archive``, a stray product that has no active subscriptions
        is modified to ``active=False`` via ``Product.modify``."""
        stray = _make_stripe_product(
            product_id="prod_stray_archive",
            metadata={"kind": "plan", "local_plan_id": "00000000-0000-0000-0000-000000000001"},
        )
        mock_price = _make_price(
            price_id="price_stray", product_id="prod_stray_archive", recurring=True
        )
        # No active subscriptions on this price.
        mock_subs = MagicMock()
        mock_subs.data = []

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stray])),
            ),
            patch(
                "stripe.Price.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([mock_price])),
            ),
            patch("stripe.Subscription.list", return_value=mock_subs),
            patch("stripe.Product.modify") as mock_modify,
        ):
            call_command("audit_stripe_catalog", "--archive", stdout=out, stderr=StringIO())

        mock_modify.assert_called_once_with("prod_stray_archive", active=False)
        assert "Archived" in out.getvalue()

    def test_stray_product_skipped_when_active_sub_exists(self, db):
        """``--archive`` must skip products whose Stripe price has at least
        one active subscription so we never archive a product still in use."""
        stray = _make_stripe_product(
            product_id="prod_in_use",
            metadata={"kind": "plan", "local_plan_id": "00000000-0000-0000-0000-000000000002"},
        )
        mock_price = _make_price(
            price_id="price_in_use", product_id="prod_in_use", recurring=True
        )
        active_sub = MagicMock()
        mock_subs = MagicMock()
        mock_subs.data = [active_sub]

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stray])),
            ),
            patch(
                "stripe.Price.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([mock_price])),
            ),
            patch("stripe.Subscription.list", return_value=mock_subs),
            patch("stripe.Product.modify") as mock_modify,
        ):
            call_command("audit_stripe_catalog", "--archive", stdout=out, stderr=StringIO())

        mock_modify.assert_not_called()
        assert "Skipping" in out.getvalue()

    def test_one_time_price_not_checked_for_subscriptions(self, db):
        """Prices with ``recurring=None`` (one-time, e.g. credit packs) cannot
        back a subscription. The command must skip the Subscription.list call
        for them and not block archiving if they are the only prices."""
        stray = _make_stripe_product(
            product_id="prod_onetime_stray",
            metadata={
                "kind": "product",
                "local_product_id": "00000000-0000-0000-0000-000000000003",
            },
        )
        onetime_price = _make_price(
            price_id="price_onetime",
            product_id="prod_onetime_stray",
            recurring=None,  # one-time price
        )

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stray])),
            ),
            patch(
                "stripe.Price.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([onetime_price])),
            ),
            patch("stripe.Subscription.list") as mock_sub_list,
            patch("stripe.Product.modify") as mock_modify,
        ):
            call_command("audit_stripe_catalog", "--archive", stdout=out, stderr=StringIO())

        # Subscription.list must NOT have been called for one-time prices.
        mock_sub_list.assert_not_called()
        mock_modify.assert_called_once_with("prod_onetime_stray", active=False)

    def test_product_with_local_product_id_is_owned(self, db):
        """A product whose metadata carries ``kind=product`` and a valid
        ``local_product_id`` matching a local Product row is counted as
        owned, not stray."""
        from apps.billing.models import Product

        local_product = Product.objects.create(
            name="Credit Pack 50", type="one_time", credits=50, is_active=True
        )
        stripe_product = _make_stripe_product(
            product_id="prod_credit_pack",
            metadata={"kind": "product", "local_product_id": str(local_product.id)},
        )

        out = StringIO()
        with (
            patch("stripe.api_key", "sk_test_dummy"),
            patch(
                "stripe.Product.list",
                return_value=MagicMock(auto_paging_iter=lambda: iter([stripe_product])),
            ),
        ):
            call_command("audit_stripe_catalog", stdout=out, stderr=StringIO())

        assert "Owned by local catalog: 1" in out.getvalue()
        assert "Stray (no matching local row): 0" in out.getvalue()
