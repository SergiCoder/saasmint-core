"""Update plans to the agreed pricing: Personal Free/Basic($19)/Pro($49),
Team Basic($17/seat)/Pro($45/seat), Boost 1($49), Boost 2($99).

Only modifies rows created by earlier seed migrations / seed_dev_data.
Skips cleanly on empty databases (e.g. test runs)."""

from django.db import migrations


def update_plans_and_prices(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanPrice = apps.get_model("billing", "PlanPrice")
    ProductPrice = apps.get_model("billing", "ProductPrice")

    # Guard: only run if seeded data exists
    if not Plan.objects.filter(name="Personal Pro").exists():
        return

    # --- Rename "Personal Pro" → "Personal Basic" ($19) ---
    Plan.objects.filter(name="Personal Pro").update(
        name="Personal Basic",
        description=(
            "For power users. Advanced analytics, priority email support, and API access."
        ),
    )
    basic_plan = Plan.objects.filter(name="Personal Basic").first()
    if basic_plan:
        PlanPrice.objects.filter(plan=basic_plan, currency="usd").update(amount=1900)
        PlanPrice.objects.filter(plan=basic_plan, currency="eur").update(amount=1800)
        PlanPrice.objects.filter(plan=basic_plan, currency="gbp").update(amount=1500)
        # Update stripe price IDs to match the new plan key
        PlanPrice.objects.filter(
            plan=basic_plan, currency="usd", stripe_price_id="price_dev_personal_pro_usd"
        ).update(stripe_price_id="price_dev_personal_basic_usd")
        PlanPrice.objects.filter(
            plan=basic_plan, currency="eur", stripe_price_id="price_dev_personal_pro_eur"
        ).update(stripe_price_id="price_dev_personal_basic_eur")
        PlanPrice.objects.filter(
            plan=basic_plan, currency="gbp", stripe_price_id="price_dev_personal_pro_gbp"
        ).update(stripe_price_id="price_dev_personal_basic_gbp")

    # --- Create new Personal Pro ($49) ---
    new_pro = Plan.objects.create(
        name="Personal Pro",
        description=(
            "Everything in Basic plus custom integrations, audit logs, and dedicated support."
        ),
        context="personal",
        interval="month",
        is_active=True,
    )
    PlanPrice.objects.create(
        plan=new_pro,
        stripe_price_id="price_dev_personal_pro_usd",
        currency="usd",
        amount=4900,
    )
    PlanPrice.objects.create(
        plan=new_pro,
        stripe_price_id="price_dev_personal_pro_eur",
        currency="eur",
        amount=4600,
    )
    PlanPrice.objects.create(
        plan=new_pro,
        stripe_price_id="price_dev_personal_pro_gbp",
        currency="gbp",
        amount=3900,
    )

    # --- Update Team Basic to $17/seat ---
    Plan.objects.filter(name="Team Basic").update(
        description="For small teams. Per-seat pricing, shared dashboards, and team analytics.",
    )
    PlanPrice.objects.filter(plan__name="Team Basic", currency="usd").update(amount=1700)
    PlanPrice.objects.filter(plan__name="Team Basic", currency="eur").update(amount=1600)
    PlanPrice.objects.filter(plan__name="Team Basic", currency="gbp").update(amount=1400)

    # --- Update Team Pro to $45/seat ---
    Plan.objects.filter(name="Team Pro").update(
        description=(
            "For growing organizations. Per-seat pricing, SSO, audit logs, and dedicated support."
        ),
    )
    PlanPrice.objects.filter(plan__name="Team Pro", currency="usd").update(amount=4500)
    PlanPrice.objects.filter(plan__name="Team Pro", currency="eur").update(amount=4200)
    PlanPrice.objects.filter(plan__name="Team Pro", currency="gbp").update(amount=3600)

    # --- Update Boost prices ---
    ProductPrice.objects.filter(product__name="Boost 1", currency="usd").update(amount=4900)
    ProductPrice.objects.filter(product__name="Boost 2", currency="usd").update(amount=9900)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0006_add_plan_description"),
    ]

    operations = [
        migrations.RunPython(update_plans_and_prices, migrations.RunPython.noop),
    ]
