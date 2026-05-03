"""Audit Stripe products against the local catalog.

Lists Stripe products whose ``metadata.local_plan_id`` / ``local_product_id``
does not match any active local row — these are leftovers from earlier
experiments. Pass ``--archive`` to set ``active=False`` on each stray product.

Read-only by default. Never deletes. Skips products with active subscriptions.
"""

from __future__ import annotations

from argparse import ArgumentParser

import stripe
from django.core.management.base import BaseCommand

from apps.billing.models import Plan, Product


def _product_metadata(sp: stripe.Product) -> dict[str, str]:
    """Return the metadata dict for a Stripe product (empty dict when absent)."""
    return (sp.metadata.to_dict() if sp.metadata else {}) or {}


class Command(BaseCommand):
    help = "List (or archive) Stripe products not present in the local catalog."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--archive",
            action="store_true",
            help="Archive stray products (active=False) after listing.",
        )

    def handle(self, *args: object, **options: object) -> None:
        if not stripe.api_key:
            self.stderr.write(self.style.ERROR("STRIPE_SECRET_KEY is not configured."))
            return

        local_plan_ids = {str(pid) for pid in Plan.objects.values_list("id", flat=True)}
        local_product_ids = {str(pid) for pid in Product.objects.values_list("id", flat=True)}

        strays: list[stripe.Product] = []
        owned = 0
        for sp in stripe.Product.list(active=True, limit=100).auto_paging_iter():
            md = _product_metadata(sp)
            kind = md.get("kind")
            local_id = md.get("local_plan_id") or md.get("local_product_id")

            if kind == "plan" and local_id in local_plan_ids:
                owned += 1
                continue
            if kind == "product" and local_id in local_product_ids:
                owned += 1
                continue
            strays.append(sp)

        self.stdout.write(f"Owned by local catalog: {owned}")
        self.stdout.write(f"Stray (no matching local row): {len(strays)}")

        if not strays:
            return

        for sp in strays:
            md = _product_metadata(sp)
            self.stdout.write(f"  · {sp.id}  name={sp.name!r}  metadata={md}")

        if not options.get("archive"):
            self.stdout.write("\nRun with --archive to set active=False on the strays above.")
            return

        for sp in strays:
            has_active_sub = False
            prices = stripe.Price.list(product=sp.id, active=True, limit=100)
            for price in prices.auto_paging_iter():
                # Stripe only supports filtering subs by recurring prices.
                # One-time prices (products like credit packs) can't back a sub.
                if price.recurring is None:
                    continue
                subs = stripe.Subscription.list(price=price.id, status="active", limit=1)
                if subs.data:
                    has_active_sub = True
                    break
            if has_active_sub:
                self.stdout.write(self.style.WARNING(f"  ! Skipping {sp.id}: active subscription"))
                continue
            stripe.Product.modify(sp.id, active=False)
            self.stdout.write(self.style.SUCCESS(f"  ✓ Archived {sp.id}"))
