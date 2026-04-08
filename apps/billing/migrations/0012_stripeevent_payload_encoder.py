"""Use DjangoJSONEncoder for StripeEvent.payload to handle Decimal/datetime."""

import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0011_add_plan_tier"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stripeevent",
            name="payload",
            field=models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
    ]
