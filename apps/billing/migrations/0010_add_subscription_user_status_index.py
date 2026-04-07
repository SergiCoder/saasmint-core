"""Add composite index on Subscription(user, status).

The new ``user`` FK column added in 0008 is queried on the hot path by:
- ``DjangoSubscriptionRepository.get_active_for_user`` (Q(user_id=...) | ...)
- ``DjangoSubscriptionRepository.delete_free_for_user`` (user_id + stripe_id)
- ``apps.billing.views.SubscriptionView.get`` (user filter + status filter)
- ``apps.billing.services.assign_free_plan`` (user existence check)

Without an index these become sequential scans on the subscriptions table.
The composite ``(user, status)`` index mirrors the existing
``idx_sub_customer_status`` and serves both the user-only existence check and
the user+status filter used by the GET endpoint and active-sub lookups.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0009_remove_multicurrency"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="subscription",
            index=models.Index(fields=["user", "status"], name="idx_sub_user_status"),
        ),
    ]
