"""Seed plans, plan prices, and boost products. Idempotent — safe to run on every deploy."""

from __future__ import annotations

from typing import TypedDict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.models import (
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    PlanTier,
    Product,
    ProductPrice,
    ProductType,
)


class _PlanSpec(TypedDict):
    name: str
    description: str
    context: PlanContext
    tier: PlanTier
    interval: PlanInterval
    amount: int


# Yearly plans are generated from monthly by charging 10x the monthly amount
# (two months free) and appending an annual-billing note to the description.
_YEARLY_DISCOUNT_MONTHS = 10
_YEARLY_DESCRIPTION_SUFFIX = " Billed annually \u2014 two months free."


_MONTHLY_PLANS: list[_PlanSpec] = [
    {
        "name": "Personal Free",
        "description": (
            "For individuals getting started. Includes basic analytics and community support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.FREE,
        "interval": PlanInterval.MONTH,
        "amount": 0,
    },
    {
        "name": "Personal Basic",
        "description": (
            "For power users. Advanced analytics, priority email support, and API access."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
        "amount": 1999,
    },
    {
        "name": "Personal Pro",
        "description": (
            "Everything in Basic plus custom integrations, audit logs, and dedicated support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
        "amount": 4999,
    },
    {
        "name": "Team Basic",
        "description": (
            "For small teams. Per-seat pricing, shared dashboards, and team analytics."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
        "amount": 1799,
    },
    {
        "name": "Team Pro",
        "description": (
            "For growing organizations. Per-seat pricing, SSO, audit logs, and dedicated support."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
        "amount": 4599,
    },
]


def _build_plans() -> list[_PlanSpec]:
    """Return monthly plans + a yearly variant for every paid monthly plan."""
    yearly: list[_PlanSpec] = [
        {
            "name": spec["name"],
            "description": spec["description"] + _YEARLY_DESCRIPTION_SUFFIX,
            "context": spec["context"],
            "tier": spec["tier"],
            "interval": PlanInterval.YEAR,
            "amount": spec["amount"] * _YEARLY_DISCOUNT_MONTHS,
        }
        for spec in _MONTHLY_PLANS
        if spec["tier"] != PlanTier.FREE
    ]
    return [*_MONTHLY_PLANS, *yearly]


PLANS: list[_PlanSpec] = _build_plans()

# (name, credit_count, amount_usd_cents)
BOOST_PRODUCTS: list[tuple[str, int, int]] = [
    ("50 Credits", 50, 499),
    ("200 Credits", 200, 1499),
    ("500 Credits", 500, 2999),
]


class Command(BaseCommand):
    help = "Seed the plan/product catalog. Idempotent."

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        self._seed_plans()
        self._seed_products()
        self.stdout.write(self.style.SUCCESS("Catalog seeded."))

    def _seed_plans(self) -> None:
        for spec in PLANS:
            plan, created = Plan.objects.get_or_create(
                context=spec["context"],
                tier=spec["tier"],
                interval=spec["interval"],
                defaults={
                    "name": spec["name"],
                    "description": spec["description"],
                    "is_active": True,
                },
            )
            if created:
                self.stdout.write(f"  + Plan: {plan.name}")

            price_id = f"price_placeholder_{spec['context']}_{spec['tier']}_{spec['interval']}"
            existing = PlanPrice.objects.filter(plan=plan).first()
            if existing is None:
                PlanPrice.objects.create(plan=plan, amount=spec["amount"], stripe_price_id=price_id)
                self.stdout.write(f"  + PlanPrice: {plan.name} = {spec['amount']}c")
            elif existing.amount != spec["amount"]:
                old = existing.amount
                existing.amount = spec["amount"]
                existing.save(update_fields=["amount"])
                self.stdout.write(f"  ✓ PlanPrice: {plan.name} {old}c → {spec['amount']}c")

    def _seed_products(self) -> None:
        for name, credit_count, amount in BOOST_PRODUCTS:
            product, created = Product.objects.get_or_create(
                name=name,
                defaults={
                    "type": ProductType.ONE_TIME,
                    "credits": credit_count,
                    "is_active": True,
                },
            )
            if created:
                self.stdout.write(f"  + Product: {name}")

            price_id = f"price_placeholder_boost_{credit_count}"
            existing = ProductPrice.objects.filter(product=product).first()
            if existing is None:
                ProductPrice.objects.create(
                    product=product, amount=amount, stripe_price_id=price_id
                )
                self.stdout.write(f"  + ProductPrice: {name} = {amount}c")
            elif existing.amount != amount:
                old = existing.amount
                existing.amount = amount
                existing.save(update_fields=["amount"])
                self.stdout.write(f"  ✓ ProductPrice: {name} {old}c → {amount}c")
