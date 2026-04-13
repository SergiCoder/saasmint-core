"""Sync local Plans/Products and their prices to Stripe.

Creates Stripe Products and Prices to mirror local catalog rows, then writes
the resulting Stripe price IDs back onto ``PlanPrice`` / ``ProductPrice``.

Idempotent: uses Stripe ``lookup_key`` to find existing prices on subsequent
runs. If amount/recurring/currency drift, the old price is archived and a new
one is created under the same Stripe Product, transferring the lookup key.
"""

from __future__ import annotations

import re
from typing import Any

import stripe
from django.core.management.base import BaseCommand

from apps.billing.models import Plan, PlanPrice, PlanTier, Product, ProductPrice

CURRENCY = "usd"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _plan_lookup_key(plan: Plan) -> str:
    tier_name = PlanTier(plan.tier).name.lower()
    return f"plan_{plan.context}_{tier_name}_{plan.interval}"


def _product_lookup_key(product: Product) -> str:
    return f"product_{_slug(product.name)}"


class Command(BaseCommand):
    help = "Create or update Stripe Products and Prices to match the local catalog."

    def handle(self, *args: object, **options: object) -> None:
        if not stripe.api_key:
            self.stderr.write(self.style.ERROR("STRIPE_SECRET_KEY is not configured."))
            return

        self._sync_plans()
        self._sync_products()
        self.stdout.write(self.style.SUCCESS("Stripe catalog sync complete."))

    # ------------------------------------------------------------------ plans

    def _sync_plans(self) -> None:
        plans = Plan.objects.filter(is_active=True).select_related("price")
        for plan in plans:
            price_row: PlanPrice | None = getattr(plan, "price", None)
            if price_row is None:
                self.stdout.write(f"  · Skipping plan {plan.name}: no PlanPrice row")
                continue
            if price_row.amount == 0:
                self.stdout.write(f"  · Skipping free plan {plan.name} (no Stripe price needed)")
                continue

            new_price_id = self._upsert_price(
                lookup_key=_plan_lookup_key(plan),
                unit_amount=price_row.amount,
                recurring={"interval": plan.interval},
                product_name=plan.name,
                product_description=plan.description or None,
                product_metadata={"local_plan_id": str(plan.id), "kind": "plan"},
                price_metadata={"local_plan_id": str(plan.id)},
            )
            self._write_price_id(price_row, new_price_id, label=f"Plan {plan.name}")

    # --------------------------------------------------------------- products

    def _sync_products(self) -> None:
        products = Product.objects.filter(is_active=True).select_related("price")
        for product in products:
            price_row: ProductPrice | None = getattr(product, "price", None)
            if price_row is None:
                self.stdout.write(f"  · Skipping product {product.name}: no ProductPrice row")
                continue

            new_price_id = self._upsert_price(
                lookup_key=_product_lookup_key(product),
                unit_amount=price_row.amount,
                recurring=None,
                product_name=product.name,
                product_description=f"{product.credits} credits",
                product_metadata={"local_product_id": str(product.id), "kind": "product"},
                price_metadata={"local_product_id": str(product.id)},
            )
            self._write_price_id(price_row, new_price_id, label=f"Product {product.name}")

    # ---------------------------------------------------------------- helpers

    def _upsert_price(
        self,
        *,
        lookup_key: str,
        unit_amount: int,
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
            if self._price_matches(current, unit_amount, recurring):
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
            "currency": CURRENCY,
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
        recurring: dict[str, Any] | None,
    ) -> bool:
        if stripe_price.unit_amount != unit_amount or stripe_price.currency != CURRENCY:
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
        self, price_row: PlanPrice | ProductPrice, new_price_id: str, *, label: str
    ) -> None:
        if new_price_id == price_row.stripe_price_id:
            self.stdout.write(f"  = {label}: already in sync ({new_price_id})")
            return
        old = price_row.stripe_price_id
        price_row.stripe_price_id = new_price_id
        price_row.save(update_fields=["stripe_price_id"])
        self.stdout.write(f"  ✓ {label}: {old} → {new_price_id}")
