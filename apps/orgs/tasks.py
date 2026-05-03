"""Celery tasks for the orgs app."""

from __future__ import annotations

import logging
from uuid import UUID

import stripe

from config.celery import app

logger = logging.getLogger(__name__)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_invitation_email_task(email: str, token: str, org_name: str, inviter_name: str) -> None:
    """Send an org invitation email via Resend (async-safe)."""
    from apps.orgs.email import send_invitation_email

    send_invitation_email(email, token, org_name, inviter_name)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def delete_org_on_subscription_cancel_task(org_id: str) -> None:
    """Hard-delete an org and its cascade off the webhook request path.

    Stripe retries any webhook handler that takes longer than ~20s, and the
    cascade (Invitation UPDATE -> User DELETE via NOT-EXISTS subquery ->
    OrgMember DELETE -> Org DELETE) holds row locks across all four
    statements. Offloading to Celery lets the webhook return immediately so
    the response is well clear of the retry threshold even for orgs with
    many members.

    Idempotent: a missing org row is a no-op, covering the
    DELETE-then-webhook race AND duplicate Stripe webhook deliveries (and a
    Celery retry of this task after a partial-success run).

    All cancel causes cascade — voluntary (owner clicked cancel) and
    involuntary (failed-payment retries exhausted, fraud, Stripe-side
    termination) collapse to the same code path. The voluntary/involuntary
    distinction was deliberately removed; do not reinstate a check on
    ``cancellation_details.reason``. See
    .claude/shared/saasmint/signup-subscription-flow.md (rule 9, discussion
    2026-04-27).
    """
    from apps.orgs.models import Org
    from apps.orgs.services import _delete_org_db_only

    org = Org.objects.filter(id=UUID(org_id)).first()
    if org is None:
        logger.info("Org %s already gone; subscription cancel is a no-op", org_id)
        return
    _delete_org_db_only(org)
    logger.info("Deleted org %s after subscription cancellation", org_id)


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def cancel_stripe_subs_task(stripe_sub_ids: list[str], org_id: str) -> None:
    """Cancel a batch of Stripe subscriptions off the request path (post org delete).

    prorate=False — org deletion is a terminal action; we don't refund the
    unused time. Already-cancelled subs (``resource_missing``) are swallowed so
    a DELETE-then-webhook race or a Celery retry is idempotent.

    Per-item failures are isolated: a transient Stripe error on sub ``B`` must
    not prevent subs ``C..N`` from being attempted in the same run. We collect
    failures and re-raise after the loop so Celery still records the failure;
    the swallow on ``resource_missing`` keeps a follow-up retry idempotent.
    """
    failures: list[stripe.StripeError] = []
    for sub_id in stripe_sub_ids:
        try:
            stripe.Subscription.cancel(sub_id, prorate=False)
        except stripe.InvalidRequestError as exc:
            if exc.code == "resource_missing":
                logger.info(
                    "Stripe sub %s already cancelled for org %s (idempotent)",
                    sub_id,
                    org_id,
                )
                continue
            logger.exception(
                "Failed to cancel Stripe sub %s for org %s",
                sub_id,
                org_id,
            )
            failures.append(exc)
        except stripe.StripeError as exc:
            logger.exception(
                "Failed to cancel Stripe sub %s for org %s",
                sub_id,
                org_id,
            )
            failures.append(exc)

    if failures:
        # Surface the first error so Celery records the failure; all sub_ids
        # were still attempted, so the partial-progress lossage is bounded.
        raise failures[0]
