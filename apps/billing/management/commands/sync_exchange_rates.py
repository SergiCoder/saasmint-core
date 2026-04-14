"""Run the sync_exchange_rates Celery task synchronously.

Useful on deploy to populate ExchangeRate rows immediately instead of
waiting for Celery Beat's first daily tick.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.billing.tasks import sync_exchange_rates


class Command(BaseCommand):
    help = "Fetch USD-based exchange rates from Stripe and persist them."

    def handle(self, *args: object, **options: object) -> None:
        sync_exchange_rates()
        self.stdout.write(self.style.SUCCESS("Exchange rates synced."))
