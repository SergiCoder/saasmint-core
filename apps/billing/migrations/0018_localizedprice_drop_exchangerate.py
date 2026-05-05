import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0017_rename_quantity_subscription_seat_limit"),
    ]

    operations = [
        migrations.DeleteModel(name="ExchangeRate"),
        migrations.CreateModel(
            name="LocalizedPrice",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("currency", models.CharField(max_length=3)),
                (
                    "amount_minor",
                    models.IntegerField(
                        help_text=(
                            "Friendly-rounded display amount in target currency's minor units."
                        )
                    ),
                ),
                ("synced_at", models.DateTimeField()),
                (
                    "plan_price",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.CASCADE,
                        related_name="localized_prices",
                        to="billing.planprice",
                    ),
                ),
                (
                    "product_price",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.CASCADE,
                        related_name="localized_prices",
                        to="billing.productprice",
                    ),
                ),
            ],
            options={
                "db_table": "localized_prices",
                "ordering": ("currency",),
            },
        ),
        migrations.AddConstraint(
            model_name="localizedprice",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("plan_price__isnull", False), ("product_price__isnull", True))
                    | models.Q(("plan_price__isnull", True), ("product_price__isnull", False))
                ),
                name="localizedprice_has_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="localizedprice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("plan_price__isnull", False)),
                fields=("plan_price", "currency"),
                name="uniq_localized_plan_price_currency",
            ),
        ),
        migrations.AddConstraint(
            model_name="localizedprice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("product_price__isnull", False)),
                fields=("product_price", "currency"),
                name="uniq_localized_product_price_currency",
            ),
        ),
    ]
