"""Seed the database with action-movie characters and orgs for local dev/test."""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from apps.billing.models import (
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    PlanTier,
    StripeCustomer,
    Subscription,
    SubscriptionStatus,
)
from apps.orgs.models import Org, OrgMember, OrgRole
from apps.users.models import AccountType, User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PERSONAL_USERS = [
    {
        "email": "jack.bauer@ctu.gov",
        "full_name": "Jack Bauer",
        "supabase_uid": "sbuid_jack_bauer",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_jack_bauer",
        "sub_stripe_id": "sub_dev_jack_bauer",
        "plan_key": "personal_pro_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "luke.hobbs@dss.gov",
        "full_name": "Luke Hobbs",
        "supabase_uid": "sbuid_luke_hobbs",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_luke_hobbs",
        "sub_stripe_id": "sub_dev_luke_hobbs",
        "plan_key": "personal_basic_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "deckard.shaw@shaw-security.com",
        "full_name": "Deckard Shaw",
        "supabase_uid": "sbuid_deckard_shaw",
        "preferred_locale": "en",
        "preferred_currency": "gbp",
        "stripe_id": "cus_dev_deckard_shaw",
        "sub_stripe_id": "sub_dev_deckard_shaw",
        "plan_key": "personal_free_monthly",
        "sub_status": SubscriptionStatus.TRIALING,
        "trial_days_from_now": 7,
    },
    {
        "email": "ethan.hunt@imf.gov",
        "full_name": "Ethan Hunt",
        "supabase_uid": "sbuid_ethan_hunt",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_ethan_hunt",
        "sub_stripe_id": "sub_dev_ethan_hunt",
        "plan_key": "personal_pro_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "james.bond@mi6.gov.uk",
        "full_name": "James Bond",
        "supabase_uid": "sbuid_james_bond",
        "preferred_locale": "en",
        "preferred_currency": "gbp",
        "stripe_id": "cus_dev_james_bond",
        "sub_stripe_id": "sub_dev_james_bond",
        "plan_key": "personal_free_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "john.mcclane@nypd.gov",
        "full_name": "John McClane",
        "supabase_uid": "sbuid_john_mcclane",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_john_mcclane",
        "sub_stripe_id": "sub_dev_john_mcclane",
        "plan_key": "personal_free_monthly",
        "sub_status": SubscriptionStatus.TRIALING,
        "trial_days_from_now": 3,
    },
    {
        "email": "jason.bourne@treadstone.com",
        "full_name": "Jason Bourne",
        "supabase_uid": "sbuid_jason_bourne",
        "preferred_locale": "en",
        "preferred_currency": "eur",
        "stripe_id": "cus_dev_jason_bourne",
        "sub_stripe_id": "sub_dev_jason_bourne",
        "plan_key": "personal_basic_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "bryan.mills@retired.com",
        "full_name": "Bryan Mills",
        "supabase_uid": "sbuid_bryan_mills",
        "preferred_locale": "fr",
        "preferred_currency": "eur",
        "stripe_id": "cus_dev_bryan_mills",
        "sub_stripe_id": "sub_dev_bryan_mills",
        "plan_key": "personal_free_monthly",
        "sub_status": SubscriptionStatus.CANCELED,
        "canceled_days_ago": 5,
    },
    {
        "email": "john.wick@continental.com",
        "full_name": "John Wick",
        "supabase_uid": "sbuid_john_wick",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_john_wick",
        "sub_stripe_id": "sub_dev_john_wick",
        "plan_key": "personal_pro_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
    {
        "email": "dominic.toretto@teamtoretto.com",
        "full_name": "Dominic Toretto",
        "supabase_uid": "sbuid_dom_toretto",
        "preferred_locale": "en",
        "preferred_currency": "usd",
        "stripe_id": "cus_dev_dom_toretto",
        "sub_stripe_id": "sub_dev_dom_toretto",
        "plan_key": "personal_free_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
    },
]

ORGS = [
    {
        "name": "Counter Terrorist Unit",
        "slug": "ctu",
        "stripe_id": "cus_dev_org_ctu",
        "sub_stripe_id": "sub_dev_org_ctu",
        "plan_key": "team_pro_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
        "seats": 5,
        "owner_email": "jack.bauer@ctu.gov",
        "members": [
            ("ethan.hunt@imf.gov", OrgRole.ADMIN),
        ],
    },
    {
        "name": "Impossible Missions Force",
        "slug": "imf",
        "stripe_id": "cus_dev_org_imf",
        "sub_stripe_id": "sub_dev_org_imf",
        "plan_key": "team_basic_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
        "seats": 3,
        "owner_email": "ethan.hunt@imf.gov",
        "members": [
            ("jason.bourne@treadstone.com", OrgRole.MEMBER),
        ],
    },
    {
        "name": "Hobbs & Shaw Security",
        "slug": "hobbs-shaw",
        "stripe_id": "cus_dev_org_hobbsshaw",
        "sub_stripe_id": "sub_dev_org_hobbsshaw",
        "plan_key": "team_pro_monthly",
        "sub_status": SubscriptionStatus.TRIALING,
        "seats": 2,
        "trial_days_from_now": 14,
        "owner_email": "luke.hobbs@dss.gov",
        "members": [
            ("deckard.shaw@shaw-security.com", OrgRole.ADMIN),
        ],
    },
    {
        "name": "The Continental",
        "slug": "continental",
        "stripe_id": "cus_dev_org_continental",
        "sub_stripe_id": "sub_dev_org_continental",
        "plan_key": "team_basic_monthly",
        "sub_status": SubscriptionStatus.ACTIVE,
        "seats": 4,
        "owner_email": "john.wick@continental.com",
        "members": [
            ("bryan.mills@retired.com", OrgRole.MEMBER),
            ("john.mcclane@nypd.gov", OrgRole.MEMBER),
        ],
    },
]

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
    help = "Seed the database with action-movie dev/test data. Safe to run multiple times."

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

        # Compute timestamps at execution time, not import time
        now = datetime.now(tz=UTC)
        self._now = now
        self._period_start = now - timedelta(days=15)
        self._period_end = now + timedelta(days=15)

        alphabet = string.ascii_letters + string.digits + string.punctuation
        self._seed_password = "".join(secrets.choice(alphabet) for _ in range(20))

        with transaction.atomic():
            plans = self._seed_plans()
            users = self._seed_users(plans)
            self._seed_orgs(plans, users)

        self.stdout.write(self.style.SUCCESS("Dev data seeded successfully."))
        self.stdout.write(f"  Seed password (all users): {self._seed_password}")

        if options.get("sync_stripe"):
            self.stdout.write("Running sync_stripe_catalog...")
            call_command("sync_stripe_catalog")

    # ------------------------------------------------------------------

    def _seed_plans(self) -> dict[str, Plan]:
        existing = {p.name: p for p in Plan.objects.filter(name__in=[p["name"] for p in PLANS])}
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
            if p["name"] not in existing
        ]
        if new_plans:
            Plan.objects.bulk_create(new_plans)
            for p in new_plans:
                self.stdout.write(f"  + Plan: {p.name}")

        all_plans = {p.name: p for p in Plan.objects.filter(name__in=[p["name"] for p in PLANS])}
        plan_map: dict[str, Plan] = {p["key"]: all_plans[p["name"]] for p in PLANS}
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

    def _seed_users(self, plans: dict[str, Plan]) -> dict[str, User]:
        user_map: dict[str, User] = {}
        for u in PERSONAL_USERS:
            user, created = User.objects.get_or_create(
                email=u["email"],
                defaults={
                    "supabase_uid": u["supabase_uid"],
                    "full_name": u["full_name"],
                    "account_type": AccountType.PERSONAL,
                    "preferred_locale": u["preferred_locale"],
                    "preferred_currency": u["preferred_currency"],
                    "is_verified": True,
                    "is_active": True,
                },
            )
            if created:
                user.set_password(self._seed_password)
                user.save(update_fields=["password"])
                self.stdout.write(f"  + User: {user.full_name} <{user.email}>")

            user_map[u["email"]] = user
            self._seed_user_billing(user, u, plans)

        return user_map

    def _seed_user_billing(self, user: User, u: dict[str, object], plans: dict[str, Plan]) -> None:
        customer, _ = StripeCustomer.objects.get_or_create(
            stripe_id=u["stripe_id"],
            defaults={"user": user, "livemode": False},
        )
        self._seed_subscription(
            stripe_id=u["sub_stripe_id"],  # type: ignore[arg-type]
            customer=customer,
            plan=plans[u["plan_key"]],  # type: ignore[index]
            status=u["sub_status"],  # type: ignore[arg-type]
            quantity=1,
            trial_days=u.get("trial_days_from_now"),  # type: ignore[arg-type]
            canceled_days_ago=u.get("canceled_days_ago"),  # type: ignore[arg-type]
        )

    def _seed_orgs(self, plans: dict[str, Plan], users: dict[str, User]) -> None:
        for o in ORGS:
            owner = users[o["owner_email"]]  # type: ignore[index]
            org, created = Org.objects.get_or_create(
                slug=o["slug"],
                defaults={"name": o["name"], "created_by": owner},
            )
            if created:
                self.stdout.write(f"  + Org: {org.name}")

            self._seed_org_memberships(org, owner, o, users)
            self._seed_org_billing(org, o, plans)

    def _seed_org_memberships(
        self,
        org: Org,
        owner: User,
        o: dict[str, object],
        users: dict[str, User],
    ) -> None:
        OrgMember.objects.get_or_create(
            org=org,
            user=owner,
            defaults={"role": OrgRole.OWNER, "is_billing": True},
        )
        for member_email, role in o.get("members", []):  # type: ignore[misc]
            member_user = users.get(member_email)
            if member_user:
                OrgMember.objects.get_or_create(
                    org=org,
                    user=member_user,
                    defaults={"role": role, "is_billing": False},
                )

    def _seed_org_billing(self, org: Org, o: dict[str, object], plans: dict[str, Plan]) -> None:
        customer, _ = StripeCustomer.objects.get_or_create(
            stripe_id=o["stripe_id"],
            defaults={"org": org, "livemode": False},
        )
        self._seed_subscription(
            stripe_id=o["sub_stripe_id"],  # type: ignore[arg-type]
            customer=customer,
            plan=plans[o["plan_key"]],  # type: ignore[index]
            status=o["sub_status"],  # type: ignore[arg-type]
            quantity=o["seats"],  # type: ignore[arg-type]
            trial_days=o.get("trial_days_from_now"),  # type: ignore[arg-type]
        )

    def _seed_subscription(
        self,
        *,
        stripe_id: str,
        customer: StripeCustomer,
        plan: Plan,
        status: SubscriptionStatus,
        quantity: int,
        trial_days: int | None = None,
        canceled_days_ago: int | None = None,
    ) -> None:
        Subscription.objects.get_or_create(
            stripe_id=stripe_id,
            defaults={
                "stripe_customer": customer,
                "status": status,
                "plan": plan,
                "quantity": quantity,
                "current_period_start": self._period_start,
                "current_period_end": self._period_end,
                "trial_ends_at": self._now + timedelta(days=trial_days) if trial_days else None,
                "canceled_at": (
                    self._now - timedelta(days=canceled_days_ago) if canceled_days_ago else None
                ),
            },
        )
