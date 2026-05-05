"""Sync local Plans/Products and their prices to Stripe, per billing currency.

For every active Plan/Product and every currency in ``settings.BILLING_CURRENCIES``,
creates Stripe Products + Prices to mirror the local catalog and writes the
resulting Stripe price IDs back onto the right local row:

- **USD**: stamped on ``PlanPrice.stripe_price_id`` / ``ProductPrice.stripe_price_id``
  (preserves the historical single-currency code path; lookup_key unchanged).
- **Non-USD billable**: stamped on ``LocalizedPrice.stripe_price_id``; lookup_key
  is suffixed with ``_{currency}`` (e.g. ``plan_personal_basic_month_eur``).

Idempotent: existing prices are matched by ``lookup_key``; if amount/currency
drift, the old price is archived and a new one is created under the same
Stripe Product, transferring the lookup key.

Bootstrap: when a non-USD ``LocalizedPrice`` row is missing for a billing
currency, this command runs ``sync_localized_prices`` inline so the ``unit_amount``
sent to Stripe is FX-correct on first deploy. If FX is unreachable the row stays
absent and the currency is skipped with a warning — the next deploy retries.
"""

from __future__ import annotations

import re
from typing import Any

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.billing.models import (
    LocalizedPrice,
    Plan,
    PlanPrice,
    PlanTier,
    Product,
    ProductPrice,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _plan_lookup_key(plan: Plan, currency: str) -> str:
    tier_name = PlanTier(plan.tier).name.lower()
    base = f"plan_{plan.context}_{tier_name}_{plan.interval}"
    return base if currency == "usd" else f"{base}_{currency}"


def _product_lookup_key(product: Product, currency: str) -> str:
    base = f"product_{_slug(product.name)}"
    return base if currency == "usd" else f"{base}_{currency}"


class Command(BaseCommand):
    help = "Create or update Stripe Products and Prices to match the local catalog."

    def handle(self, *args: object, **options: object) -> None:
        if not stripe.api_key:
            self.stderr.write(self.style.ERROR("STRIPE_SECRET_KEY is not configured."))
            return

        currencies: list[str] = list(settings.BILLING_CURRENCIES)
        for currency in currencies:
            self.stdout.write(f"— {currency.upper()} —")
            self._sync_plans(currency)
            self._sync_products(currency)
        self.stdout.write(self.style.SUCCESS("Stripe catalog sync complete."))

    # ------------------------------------------------------------------ plans

    def _sync_plans(self, currency: str) -> None:
        plans = Plan.objects.filter(is_active=True).select_related("price")
        for plan in plans:
            price_row: PlanPrice | None = getattr(plan, "price", None)
            if price_row is None:
                self.stdout.write(f"  · Skipping plan {plan.name}: no PlanPrice row")
                continue

            unit_amount = self._unit_amount_for(price_row, currency, owner_kwarg="plan_price_id")
            if unit_amount is None:
                # Bootstrap couldn't produce a localized amount (FX feed down on
                # first deploy). Skip this currency this run; next deploy retries.
                continue

            new_price_id = self._upsert_price(
                lookup_key=_plan_lookup_key(plan, currency),
                unit_amount=unit_amount,
                currency=currency,
                recurring={"interval": plan.interval},
                product_name=plan.name,
                product_description=plan.description or None,
                product_metadata={"local_plan_id": str(plan.id), "kind": "plan"},
                price_metadata={"local_plan_id": str(plan.id)},
            )
            self._write_price_id(
                price_row, new_price_id, currency=currency, label=f"Plan {plan.name}"
            )

    # --------------------------------------------------------------- products

    def _sync_products(self, currency: str) -> None:
        products = Product.objects.filter(is_active=True).select_related("price")
        for product in products:
            price_row: ProductPrice | None = getattr(product, "price", None)
            if price_row is None:
                self.stdout.write(f"  · Skipping product {product.name}: no ProductPrice row")
                continue

            unit_amount = self._unit_amount_for(
                price_row, currency, owner_kwarg="product_price_id"
            )
            if unit_amount is None:
                continue

            new_price_id = self._upsert_price(
                lookup_key=_product_lookup_key(product, currency),
                unit_amount=unit_amount,
                currency=currency,
                recurring=None,
                product_name=product.name,
                product_description=f"{product.credits} credits",
                product_metadata={"local_product_id": str(product.id), "kind": "product"},
                price_metadata={"local_product_id": str(product.id)},
            )
            self._write_price_id(
                price_row, new_price_id, currency=currency, label=f"Product {product.name}"
            )

    # ---------------------------------------------------------------- helpers

    def _unit_amount_for(
        self,
        price_row: PlanPrice | ProductPrice,
        currency: str,
        *,
        owner_kwarg: str,
    ) -> int | None:
        """Resolve the Stripe Price ``unit_amount`` for *price_row* in *currency*.

        USD reads ``price_row.amount`` directly (source-of-truth USD cents).
        Non-USD reads the matching ``LocalizedPrice.amount_minor``. If the
        localized row is missing this is the bootstrap case — run
        ``sync_localized_prices`` inline once and retry; if FX is still
        unreachable, return ``None`` so the caller skips this currency.
        """
        if currency == "usd":
            return price_row.amount

        owner_filter = {owner_kwarg: price_row.id, "currency": currency}
        existing = LocalizedPrice.objects.filter(**owner_filter).only("amount_minor").first()
        if existing is not None:
            return existing.amount_minor

        # Bootstrap: try to populate the row via the FX feed, then re-query.
        from apps.billing.tasks import sync_localized_prices

        sync_localized_prices()
        existing = LocalizedPrice.objects.filter(**owner_filter).only("amount_minor").first()
        if existing is None:
            self.stdout.write(
                f"  · Skipping {currency.upper()}: no LocalizedPrice row "
                f"(FX feed unreachable on bootstrap)"
            )
            return None
        return existing.amount_minor

    def _upsert_price(
        self,
        *,
        lookup_key: str,
        unit_amount: int,
        currency: str,
        recurring: dict[str, Any] | None,
        product_name: str,
        product_description: str | None,
        product_metadata: dict[str, str],
        price_metadata: dict[str, str],
    ) -> str:
        existing = stripe.Price.list(lookup_keys=[lookup_key], limit=1, expand=["data.product"])
        product_id: str | None = None

        if existing.data:
            current = existing.data[0]
            current_product = current.product
            if self._price_matches(current, unit_amount, currency, recurring):
                self._sync_stripe_product(
                    current_product, product_name, product_description, product_metadata
                )
                return current.id

            # Reuse the existing Stripe Product but archive the stale Price.
            product_id = (
                current_product.id
                if isinstance(current_product, stripe.Product)
                else str(current_product)
            )
            stripe.Price.modify(current.id, active=False)
            self._sync_stripe_product(
                product_id, product_name, product_description, product_metadata
            )

        if product_id is None:
            create_product_kwargs: dict[str, Any] = {
                "name": product_name,
                "metadata": product_metadata,
            }
            if product_description:
                create_product_kwargs["description"] = product_description
            stripe_product = stripe.Product.create(**create_product_kwargs)
            product_id = stripe_product.id

        create_price_kwargs: dict[str, Any] = {
            "product": product_id,
            "unit_amount": unit_amount,
            "currency": currency,
            "lookup_key": lookup_key,
            "transfer_lookup_key": True,
            "metadata": price_metadata,
        }
        if recurring is not None:
            create_price_kwargs["recurring"] = recurring
        new_price = stripe.Price.create(**create_price_kwargs)
        return new_price.id

    @staticmethod
    def _price_matches(
        stripe_price: stripe.Price,
        unit_amount: int,
        currency: str,
        recurring: dict[str, Any] | None,
    ) -> bool:
        if stripe_price.unit_amount != unit_amount or stripe_price.currency != currency:
            return False
        current_recurring = stripe_price.recurring
        if recurring is None:
            return current_recurring is None
        if current_recurring is None:
            return False
        return bool(current_recurring.interval == recurring["interval"])

    def _sync_stripe_product(
        self,
        product_or_id: stripe.Product | str,
        name: str,
        description: str | None,
        metadata: dict[str, str],
    ) -> None:
        existing_metadata: dict[str, str] = {}
        if isinstance(product_or_id, stripe.Product):
            product_id = product_or_id.id
            existing_name: str | None = product_or_id.name
            existing_description: str | None = product_or_id.description
            raw_metadata = product_or_id.metadata
            if raw_metadata:
                # ``UntypedStripeObject`` exposes attributes/keys via ``to_dict()``.
                existing_metadata = {str(k): str(v) for k, v in raw_metadata.to_dict().items()}
        else:
            product_id = product_or_id
            existing_name = None
            existing_description = None

        update: dict[str, Any] = {}
        if existing_name is not None and existing_name != name:
            update["name"] = name
        if description and existing_description != description:
            update["description"] = description
        merged_metadata = {**existing_metadata, **metadata}
        if merged_metadata != existing_metadata:
            update["metadata"] = merged_metadata
        if update:
            stripe.Product.modify(product_id, **update)

    def _write_price_id(
        self,
        price_row: PlanPrice | ProductPrice,
        new_price_id: str,
        *,
        currency: str,
        label: str,
    ) -> None:
        """Stamp *new_price_id* onto the right column.

        USD lives on ``price_row.stripe_price_id`` (existing column). Non-USD
        lives on ``LocalizedPrice.stripe_price_id`` for the matching
        (price_row, currency) pair.
        """
        full_label = f"{label} [{currency.upper()}]"
        if currency == "usd":
            current_id = price_row.stripe_price_id
            if new_price_id == current_id:
                self.stdout.write(f"  = {full_label}: already in sync ({new_price_id})")
                return
            price_row.stripe_price_id = new_price_id
            price_row.save(update_fields=["stripe_price_id"])
            self.stdout.write(f"  ✓ {full_label}: {current_id} → {new_price_id}")
            return

        owner_kwargs: dict[str, Any] = (
            {"plan_price_id": price_row.id}
            if isinstance(price_row, PlanPrice)
            else {"product_price_id": price_row.id}
        )
        localized = LocalizedPrice.objects.filter(currency=currency, **owner_kwargs).first()
        if localized is None:
            # Should be unreachable: _unit_amount_for already triggered bootstrap.
            self.stdout.write(
                f"  ! {full_label}: LocalizedPrice row vanished mid-sync; skipping"
            )
            return
        if localized.stripe_price_id == new_price_id:
            self.stdout.write(f"  = {full_label}: already in sync ({new_price_id})")
            return
        old = localized.stripe_price_id
        localized.stripe_price_id = new_price_id
        localized.save(update_fields=["stripe_price_id"])
        self.stdout.write(f"  ✓ {full_label}: {old} → {new_price_id}")
