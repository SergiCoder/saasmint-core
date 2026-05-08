# Generated manually to drop a non-selective index on a hot-write column.
# `password_changed_at` is read via attribute access on an already-fetched
# User row (PK lookup in JWTAuthentication), never as a queryset filter,
# so the index added in 0012 only adds write overhead on password changes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_user_password_changed_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='password_changed_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Last password reset/change. Access tokens minted before this moment are rejected by JWTAuthentication.',
                null=True,
            ),
        ),
    ]
