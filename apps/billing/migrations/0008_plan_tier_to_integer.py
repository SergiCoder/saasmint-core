"""Convert Plan.tier from CharField (free/basic/pro) to IntegerField (1/2/3)."""

from django.db import migrations, models

TIER_MAP = {"free": "1", "basic": "2", "pro": "3"}


def convert_tier_to_int(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for old_val, new_val in TIER_MAP.items():
        Plan.objects.filter(tier=old_val).update(tier=new_val)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0007_seed_boost_products"),
    ]

    operations = [
        # 1. Drop the unique constraint that references the tier column
        migrations.RemoveConstraint(
            model_name="plan",
            name="uniq_active_plan_per_context_tier_interval",
        ),
        # 2. Convert string values to their integer equivalents (still stored as strings in a CharField)
        migrations.RunPython(convert_tier_to_int, migrations.RunPython.noop),
        # 3. Change the column type from CharField to IntegerField
        migrations.AlterField(
            model_name="plan",
            name="tier",
            field=models.IntegerField(choices=[(1, "Free"), (2, "Basic"), (3, "Pro")], default=2),
        ),
        # 4. Re-add the unique constraint
        migrations.AddConstraint(
            model_name="plan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("context", "tier", "interval"),
                name="uniq_active_plan_per_context_tier_interval",
            ),
        ),
    ]
