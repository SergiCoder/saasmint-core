"""Drop the dual-shape Subscription: delete free rows, drop the free-only constraint.

Subscription becomes a pure Stripe mirror — every row has a stripe_id. The free
tier is now represented by the *absence* of a Subscription rather than a row
with stripe_id IS NULL. Existing free Subscription rows and their seeded
free Plan are removed; the partial unique constraint that guarded "at most one
free sub per user" is dropped because no free rows exist any more.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations

# Batch size for the free-subscription cleanup. Every signup historically wrote
# a free Subscription row, so this table can be the largest in the schema —
# delete in chunks to keep transaction size bounded (WAL pressure, lock count,
# and replication lag) instead of a single unbounded DELETE.
_DELETE_BATCH_SIZE = 1000


def _delete_free_rows(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401, ARG001
    Subscription = apps.get_model("billing", "Subscription")
    Plan = apps.get_model("billing", "Plan")

    # Free Subscriptions are exactly the rows without a Stripe id. Delete in
    # batches so an arbitrarily large free-tier population doesn't produce a
    # single multi-GB transaction.
    while True:
        ids = list(
            Subscription.objects.filter(stripe_id__isnull=True).values_list("pk", flat=True)[
                :_DELETE_BATCH_SIZE
            ]
        )
        if not ids:
            break
        Subscription.objects.filter(pk__in=ids).delete()

    # Free Plan rows (tier=1, context="personal") are unreachable now that
    # assign_free_plan and the free seed entry are gone; delete them here,
    # after the free Subscriptions above, so the PROTECT FK on
    # Subscription.plan no longer guards them.
    # This set is small (a single row per interval at most) so no batching needed.
    Plan.objects.filter(tier=1, context="personal").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0013_creditbalance_credittransaction"),
    ]

    operations = [
        # Reverse is intentionally a no-op: free Subscription/Plan rows cannot
        # be reconstructed from the post-migration state. A backwards migrate
        # rolls the schema back but leaves the data deleted; restore from
        # backup if a real rollback is needed.
        migrations.RunPython(_delete_free_rows, reverse_code=migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="subscription",
            name="uniq_free_subscription_per_user",
        ),
    ]
