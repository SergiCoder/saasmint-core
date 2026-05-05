"""Tests for the ``sync_localized_prices`` management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.billing.tests.conftest import fx_response, seed_plan_price

pytestmark = pytest.mark.django_db


class TestSyncLocalizedPricesCommand:
    def test_command_writes_rows_and_prints_count(self):
        """Happy path: rows are created and the success message includes the
        row count returned by the underlying task."""
        from apps.billing.models import LocalizedPrice

        seed_plan_price(999)
        stdout = StringIO()
        with patch("apps.billing.tasks.httpx.get", return_value=fx_response({"eur": 0.9})):
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

        seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=fx_response({"eur": 0.9})):
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
