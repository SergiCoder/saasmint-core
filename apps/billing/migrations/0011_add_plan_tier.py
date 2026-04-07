"""Add `tier` to Plan, backfill from name, and add unique active-plan constraint."""

from django.db import migrations, models


def backfill_tier_from_name(apps, schema_editor):
    """Infer tier from existing plan names.

    Rules:
    - name contains "Free" (case-insensitive) → free
    - name contains "Pro"  (case-insensitive) → pro
    - anything else → basic (the field default)
    """
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name__icontains="Free").update(tier="free")
    Plan.objects.filter(name__icontains="Pro").update(tier="pro")


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0010_add_subscription_user_status_index"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="plan",
            options={"ordering": ("context", "tier", "interval")},
        ),
        migrations.AddField(
            model_name="plan",
            name="tier",
            field=models.CharField(
                choices=[("free", "Free"), ("basic", "Basic"), ("pro", "Pro")],
                default="basic",
                max_length=10,
            ),
        ),
        migrations.RunPython(backfill_tier_from_name, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="plan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("context", "tier", "interval"),
                name="uniq_active_plan_per_context_tier_interval",
            ),
        ),
    ]
