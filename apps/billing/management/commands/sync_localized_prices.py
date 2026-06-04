"""Run the sync_localized_prices Celery task synchronously.

Useful on deploy to populate ``LocalizedPrice`` rows immediately instead of
waiting for Celery Beat's first daily tick. Idempotent — safe to re-run.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.billing.tasks import sync_localized_prices


class Command(BaseCommand):
    help = "Recompute LocalizedPrice rows for every (PlanPrice|ProductPrice, currency)."

    def handle(self, *args: object, **options: object) -> None:
        changed = sync_localized_prices()
        self.stdout.write(self.style.SUCCESS(f"Localized prices synced: {changed} rows changed."))
