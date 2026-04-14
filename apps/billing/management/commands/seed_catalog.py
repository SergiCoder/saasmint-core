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


PLANS: list[_PlanSpec] = [
    {
        "name": "Personal Free",
        "description": (
            "For individuals getting started. Includes basic analytics and community support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.FREE,
        "interval": PlanInterval.MONTH,
    },
    {
        "name": "Personal Basic",
        "description": (
            "For power users. Advanced analytics, priority email support, and API access."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
    },
    {
        "name": "Personal Pro",
        "description": (
            "Everything in Basic plus custom integrations, audit logs, and dedicated support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
    },
    {
        "name": "Team Basic",
        "description": (
            "For small teams. Per-seat pricing, shared dashboards, and team analytics."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
    },
    {
        "name": "Team Pro",
        "description": (
            "For growing organizations. Per-seat pricing, SSO, audit logs, and dedicated support."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
    },
    {
        "name": "Personal Basic",
        "description": (
            "For power users."
            " Advanced analytics, priority email support, and API access."
            " Billed annually \u2014 two months free."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.YEAR,
    },
    {
        "name": "Personal Pro",
        "description": (
            "Everything in Basic plus custom integrations,"
            " audit logs, and dedicated support."
            " Billed annually \u2014 two months free."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.YEAR,
    },
    {
        "name": "Team Basic",
        "description": (
            "For small teams."
            " Per-seat pricing, shared dashboards, and team analytics."
            " Billed annually \u2014 two months free."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.YEAR,
    },
    {
        "name": "Team Pro",
        "description": (
            "For growing organizations."
            " Per-seat pricing, SSO, audit logs, and dedicated support."
            " Billed annually \u2014 two months free."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.YEAR,
    },
]

# (context, tier, interval, amount_usd_cents)
PLAN_PRICES: list[tuple[str, int, str, int]] = [
    (PlanContext.PERSONAL, PlanTier.FREE, PlanInterval.MONTH, 0),
    (PlanContext.PERSONAL, PlanTier.BASIC, PlanInterval.MONTH, 1900),
    (PlanContext.PERSONAL, PlanTier.PRO, PlanInterval.MONTH, 4900),
    (PlanContext.TEAM, PlanTier.BASIC, PlanInterval.MONTH, 1700),
    (PlanContext.TEAM, PlanTier.PRO, PlanInterval.MONTH, 4500),
    (PlanContext.PERSONAL, PlanTier.BASIC, PlanInterval.YEAR, 19000),
    (PlanContext.PERSONAL, PlanTier.PRO, PlanInterval.YEAR, 49000),
    (PlanContext.TEAM, PlanTier.BASIC, PlanInterval.YEAR, 17000),
    (PlanContext.TEAM, PlanTier.PRO, PlanInterval.YEAR, 45000),
]

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

        # Load all active plans once to avoid a get() per PLAN_PRICES row.
        plans_by_identity: dict[tuple[str, int, str], Plan] = {
            (p.context, p.tier, p.interval): p
            for p in Plan.objects.filter(is_active=True)
        }
        for context, tier, interval, amount in PLAN_PRICES:
            plan = plans_by_identity[context, tier, interval]
            price_id = f"price_placeholder_{context}_{tier}_{interval}"
            _, created = PlanPrice.objects.get_or_create(
                plan=plan,
                defaults={"amount": amount, "stripe_price_id": price_id},
            )
            if created:
                self.stdout.write(f"  + PlanPrice: {plan.name} = {amount}c")

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
            _, price_created = ProductPrice.objects.get_or_create(
                product=product,
                defaults={
                    "amount": amount,
                    "stripe_price_id": price_id,
                },
            )
            if price_created:
                self.stdout.write(f"  + ProductPrice: {name} = {amount}c")
