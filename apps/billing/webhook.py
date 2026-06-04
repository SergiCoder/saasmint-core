"""Stripe webhook endpoint — bypasses DRF auth, verified by Stripe signature."""

from __future__ import annotations

import json
import logging

import stripe
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing.models import StripeEvent as StripeEventModel
from apps.billing.tasks import process_stripe_webhook

logger = logging.getLogger(__name__)

# Stripe's documented webhook payload ceiling is far below this; the cap is a
# belt-and-braces guard against an attacker submitting a giant body to force
# the process to buffer it before signature verification.
_MAX_WEBHOOK_BODY = 256 * 1024  # 256 KB


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    """Verify signature, persist the event synchronously, enqueue async processing.

    The task only receives the event's primary key — the verified payload is
    read from the DB row. This keeps PII out of the Celery queue and lets a
    task retry after a webhook-secret rotation without re-verifying.
    """
    # Reject oversized bodies before reading them. `request.body` would buffer
    # the full payload into memory otherwise.
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        content_length = 0
    if content_length > _MAX_WEBHOOK_BODY:
        logger.warning(
            "Stripe webhook rejected: Content-Length %d exceeds %d",
            content_length,
            _MAX_WEBHOOK_BODY,
        )
        return HttpResponse(status=413)

    payload = request.body
    if len(payload) > _MAX_WEBHOOK_BODY:
        logger.warning("Stripe webhook rejected: body length %d exceeds cap", len(payload))
        return HttpResponse(status=413)
    signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)  # type: ignore[no-untyped-call]  # Stripe stub missing return type annotation
    except stripe.SignatureVerificationError:
        logger.error("Stripe webhook signature verification failed")
        return HttpResponse(status=400)
    except ValueError:
        logger.warning("Stripe webhook payload is invalid JSON")
        return HttpResponse(status=400)

    try:
        event_dict = json.loads(payload)
        stripe_id = str(event_dict["id"])
        event_type = str(event_dict["type"])
        livemode = bool(event_dict["livemode"])
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Stripe webhook payload missing required fields")
        return HttpResponse(status=400)

    # Reject events whose livemode doesn't match the current Stripe key.
    # Prevents a replayed test event from being processed against the prod key
    # (and vice versa). Drop silently with 202 — the mismatch is permanent.
    # Accept both standard and restricted-key prefixes so a deployment using
    # an ``rk_live_`` restricted key is still recognised as live mode.
    key_is_live = settings.STRIPE_SECRET_KEY.startswith(("sk_live_", "rk_live_"))
    if livemode != key_is_live:
        logger.error(
            "Stripe webhook livemode mismatch for event %s (livemode=%s, key_is_live=%s) — drop.",
            stripe_id,
            livemode,
            key_is_live,
        )
        return HttpResponse(status=202)

    # Wrap the get_or_create + Celery dispatch in a transaction so the
    # ``on_commit`` hook only fires after the StripeEvent row is durably
    # persisted. If the dispatch ran in autocommit it could enqueue a task
    # against an event row that the surrounding request is about to roll
    # back (e.g. via a downstream middleware), leaving Celery chasing an id
    # that doesn't exist.
    with transaction.atomic():
        event_row, created = StripeEventModel.objects.get_or_create(
            stripe_id=stripe_id,
            defaults={
                "type": event_type,
                "livemode": livemode,
                "payload": event_dict,
            },
        )
        if not created:
            logger.info("Skipping duplicate Stripe event %s", stripe_id)
            return HttpResponse(status=202)

        event_row_id = str(event_row.id)
        transaction.on_commit(lambda: process_stripe_webhook.delay(event_row_id))

    return HttpResponse(status=202)
