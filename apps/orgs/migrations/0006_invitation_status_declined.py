from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0005_add_org_is_active"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invitation",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("expired", "Expired"),
                    ("cancelled", "Cancelled"),
                    ("declined", "Declined"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
