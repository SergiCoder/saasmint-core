from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0008_performance_audit_indexes"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="org",
            name="idx_orgs_slug_active",
        ),
        migrations.RemoveField(
            model_name="org",
            name="deleted_at",
        ),
        migrations.AddConstraint(
            model_name="org",
            constraint=models.UniqueConstraint(
                fields=("slug",),
                name="idx_orgs_slug_active",
            ),
        ),
    ]
