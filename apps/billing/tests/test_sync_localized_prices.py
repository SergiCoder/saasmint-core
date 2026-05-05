"""Tests for the ``sync_localized_prices`` management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def _fx_response(rates: dict[str, float]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "result": "success",
        "rates": {k.upper(): v for k, v in rates.items()},
    }
    return resp


def _seed_plan_price(amount: int = 999) -> object:
    from apps.billing.models import Plan, PlanPrice

    plan = Plan.objects.create(
        name="Pro Monthly", context="personal", tier=3, interval="month"
    )
    return PlanPrice.objects.create(plan=plan, stripe_price_id=f"price_{plan.id}", amount=amount)


class TestSyncLocalizedPricesCommand:
    def test_command_writes_rows_and_prints_count(self):
        """Happy path: rows are created and the success message includes the
        row count returned by the underlying task."""
        from apps.billing.models import LocalizedPrice

        _seed_plan_price(999)
        stdout = StringIO()
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            call_command("sync_localized_prices", stdout=stdout)

        assert LocalizedPrice.objects.filter(currency="eur").exists()
        output = stdout.getvalue()
        assert "synced" in output.lower()
        # The command outputs the row count returned by sync_localized_prices().
        written = LocalizedPrice.objects.count()
        assert str(written) in output

    def test_command_is_idempotent(self):
        """Running the command twice must not create duplicate rows."""
        from apps.billing.models import LocalizedPrice

        _seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            call_command("sync_localized_prices", stdout=StringIO())
            count_first = LocalizedPrice.objects.count()
            call_command("sync_localized_prices", stdout=StringIO())
            count_second = LocalizedPrice.objects.count()

        assert count_first == count_second

    def test_command_exits_cleanly_when_api_is_down(self):
        """An upstream HTTP error must not raise — the command should print
        the success line with 0 rows and exit normally."""
        import httpx

        stdout = StringIO()
        with patch(
            "apps.billing.tasks.httpx.get", side_effect=httpx.HTTPError("connection refused")
        ):
            # Should not raise.
            call_command("sync_localized_prices", stdout=stdout)

        output = stdout.getvalue()
        assert "0" in output
