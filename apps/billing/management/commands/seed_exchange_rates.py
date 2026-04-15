"""Seed exchange rates from a public API for local development.

Fetches current USD-based rates from the Open Exchange Rates API and
populates the ``exchange_rates`` table.  Useful when the Stripe Exchange
Rates API is unavailable (test-mode keys).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
from django.core.management.base import BaseCommand
from saasmint_core.services.currency import SUPPORTED_CURRENCIES

from apps.billing.models import ExchangeRate

API_URL = "https://open.er-api.com/v6/latest/USD"


class Command(BaseCommand):
    help = "Seed exchange rates from a public API (for dev/test environments)."

    def handle(self, *args: object, **options: object) -> None:
        self.stdout.write("Fetching rates from open.er-api.com …")

        resp = httpx.get(API_URL, timeout=httpx.Timeout(10.0))
        resp.raise_for_status()
        data = resp.json()

        if data.get("result") != "success":
            self.stderr.write(self.style.ERROR(f"API error: {data}"))
            return

        api_rates: dict[str, float] = data["rates"]
        now = datetime.now(UTC)

        rows: list[ExchangeRate] = []
        for currency in sorted(SUPPORTED_CURRENCIES):
            if currency == "usd":
                continue
            rate = api_rates.get(currency.upper())
            if rate is None:
                self.stderr.write(self.style.WARNING(f"  No rate for {currency.upper()}, skipping"))
                continue
            rows.append(ExchangeRate(currency=currency, rate=Decimal(str(rate)), fetched_at=now))

        if rows:
            ExchangeRate.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["currency"],
                update_fields=["rate", "fetched_at"],
            )

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(rows)} exchange rates."))
        for er in ExchangeRate.objects.all().order_by("currency"):
            self.stdout.write(f"  {er}")
