"""Remove multi-currency support from PlanPrice and ProductPrice.

Keep only USD rows, drop the currency column, and convert ForeignKey → OneToOneField
so each plan/product has exactly one price.
"""

import django.db.models.deletion
from django.db import migrations, models


def delete_non_usd_prices(apps, schema_editor):
    PlanPrice = apps.get_model("billing", "PlanPrice")
    ProductPrice = apps.get_model("billing", "ProductPrice")
    PlanPrice.objects.exclude(currency="usd").delete()
    ProductPrice.objects.exclude(currency="usd").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0008_make_subscription_stripe_fields_nullable"),
    ]

    operations = [
        # 1. Delete non-USD price rows
        migrations.RunPython(delete_non_usd_prices, migrations.RunPython.noop),
        # 2. Remove the unique constraints that reference currency
        migrations.RemoveConstraint(
            model_name="planprice",
            name="plan_prices_plan_currency_uniq",
        ),
        migrations.RemoveConstraint(
            model_name="productprice",
            name="product_prices_product_currency_uniq",
        ),
        # 3. Drop the currency columns
        migrations.RemoveField(
            model_name="planprice",
            name="currency",
        ),
        migrations.RemoveField(
            model_name="productprice",
            name="currency",
        ),
        # 4. Convert ForeignKey → OneToOneField (enforces one price per plan/product)
        migrations.AlterField(
            model_name="planprice",
            name="plan",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="price",
                to="billing.plan",
            ),
        ),
        migrations.AlterField(
            model_name="productprice",
            name="product",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="price",
                to="billing.product",
            ),
        ),
    ]
