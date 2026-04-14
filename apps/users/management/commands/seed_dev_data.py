"""Seed plans, products, and prices for local dev/test."""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from apps.billing.models import Plan, PlanContext, PlanInterval, PlanPrice, PlanTier

PLANS = [
    {
        "key": "personal_free_monthly",
        "name": "Personal Free",
        "description": (
            "For individuals getting started. Includes basic analytics and community support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.FREE,
        "interval": PlanInterval.MONTH,
    },
    {
        "key": "personal_basic_monthly",
        "name": "Personal Basic",
        "description": (
            "For power users. Advanced analytics, priority email support, and API access."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
    },
    {
        "key": "personal_pro_monthly",
        "name": "Personal Pro",
        "description": (
            "Everything in Basic plus custom integrations, audit logs, and dedicated support."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
    },
    {
        "key": "team_basic_monthly",
        "name": "Team Basic",
        "description": (
            "For small teams. Per-seat pricing, shared dashboards, and team analytics."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.MONTH,
    },
    {
        "key": "team_pro_monthly",
        "name": "Team Pro",
        "description": (
            "For growing organizations. Per-seat pricing, SSO, audit logs, and dedicated support."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.MONTH,
    },
    {
        "key": "personal_basic_yearly",
        "name": "Personal Basic",
        "description": (
            "For power users. Advanced analytics, priority email support, and API access. "
            "Billed annually — two months free."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.YEAR,
    },
    {
        "key": "personal_pro_yearly",
        "name": "Personal Pro",
        "description": (
            "Everything in Basic plus custom integrations, audit logs, and dedicated support. "
            "Billed annually — two months free."
        ),
        "context": PlanContext.PERSONAL,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.YEAR,
    },
    {
        "key": "team_basic_yearly",
        "name": "Team Basic",
        "description": (
            "For small teams. Per-seat pricing, shared dashboards, and team analytics. "
            "Billed annually — two months free."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.BASIC,
        "interval": PlanInterval.YEAR,
    },
    {
        "key": "team_pro_yearly",
        "name": "Team Pro",
        "description": (
            "For growing organizations. Per-seat pricing, SSO, audit logs, and dedicated support. "
            "Billed annually — two months free."
        ),
        "context": PlanContext.TEAM,
        "tier": PlanTier.PRO,
        "interval": PlanInterval.YEAR,
    },
]

# (plan_key, amount_usd_cents, stripe_price_id)
# Yearly prices = monthly * 10 (two months free).
PLAN_PRICES = [
    ("personal_free_monthly", 0, "price_dev_personal_free_usd"),
    ("personal_basic_monthly", 1900, "price_dev_personal_basic_usd"),
    ("personal_pro_monthly", 4900, "price_dev_personal_pro_usd"),
    ("team_basic_monthly", 1700, "price_dev_team_basic_usd"),
    ("team_pro_monthly", 4500, "price_dev_team_pro_usd"),
    ("personal_basic_yearly", 19000, "price_dev_personal_basic_yearly_usd"),
    ("personal_pro_yearly", 49000, "price_dev_personal_pro_yearly_usd"),
    ("team_basic_yearly", 17000, "price_dev_team_basic_yearly_usd"),
    ("team_pro_yearly", 45000, "price_dev_team_pro_yearly_usd"),
]


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

        with transaction.atomic():
            self._seed_plans()

        self.stdout.write(self.style.SUCCESS("Dev data seeded successfully."))

        if options.get("sync_stripe"):
            self.stdout.write("Running sync_stripe_catalog...")
            call_command("sync_stripe_catalog")

    # ------------------------------------------------------------------

    def _seed_plans(self) -> dict[str, Plan]:
        # Identity is (context, tier, interval) — multiple plans can share a name
        # (e.g. monthly and yearly variants).
        def identity_obj(p: Plan) -> tuple[str, int, str]:
            return (p.context, p.tier, p.interval)

        def identity_spec(p: dict[str, object]) -> tuple[str, int, str]:
            return (p["context"], p["tier"], p["interval"])  # type: ignore[return-value]

        all_plans: dict[tuple[str, int, str], Plan] = {
            identity_obj(p): p for p in Plan.objects.filter(is_active=True)
        }
        new_plans = [
            Plan(
                name=p["name"],
                description=p["description"],
                context=p["context"],
                tier=p["tier"],
                interval=p["interval"],
                is_active=True,
            )
            for p in PLANS
            if identity_spec(p) not in all_plans
        ]
        if new_plans:
            Plan.objects.bulk_create(new_plans)
            for p in new_plans:
                self.stdout.write(f"  + Plan: {p.name}")
                all_plans[identity_obj(p)] = p

        plan_map: dict[str, Plan] = {p["key"]: all_plans[identity_spec(p)] for p in PLANS}
        self._seed_plan_prices(plan_map)
        return plan_map

    def _seed_plan_prices(self, plan_map: dict[str, Plan]) -> None:
        existing_plan_ids = set(
            PlanPrice.objects.filter(plan__in=plan_map.values()).values_list("plan_id", flat=True)
        )
        new_prices = [
            PlanPrice(
                plan=plan_map[plan_key],
                stripe_price_id=stripe_price_id,
                amount=amount,
            )
            for plan_key, amount, stripe_price_id in PLAN_PRICES
            if plan_map[plan_key].pk not in existing_plan_ids
        ]
        if new_prices:
            PlanPrice.objects.bulk_create(new_prices)
