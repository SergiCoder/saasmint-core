"""Seed plans, products, and prices for local dev/test."""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Seed plans, products, and prices for local dev/test. Safe to run multiple times."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--sync-stripe",
            action="store_true",
            help="After seeding, run sync_stripe_catalog to push plans/products to Stripe.",
        )

    def handle(self, *args: object, **options: object) -> None:
        from django.conf import settings

        if not settings.DEBUG:
            self.stderr.write(self.style.ERROR("seed_dev_data can only run with DEBUG=True"))
            return

        call_command("seed_catalog")

        self.stdout.write(self.style.SUCCESS("Dev data seeded successfully."))

        if options.get("sync_stripe"):
            self.stdout.write("Running sync_stripe_catalog...")
            call_command("sync_stripe_catalog")
