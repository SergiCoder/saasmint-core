from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0009_remove_org_deleted_at"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="org",
            name="is_active",
        ),
    ]
