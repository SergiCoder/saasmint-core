"""Stripe webhook endpoint — bypasses DRF auth, verified by Stripe signature."""

from __future__ import annotations

import logging

import stripe
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing.tasks import process_stripe_webhook

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    """Verify Stripe signature and dispatch to Celery for async processing."""
    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    # Verify signature synchronously to reject invalid payloads before queuing.
    # Core's handle_stripe_event also verifies — the duplicate is intentional to
    # avoid filling the Celery queue with unverifiable payloads.
    try:
        stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)  # type: ignore[no-untyped-call]
    except stripe.SignatureVerificationError:
        logger.error("Stripe webhook signature verification failed")
        return HttpResponse(status=400)
    except ValueError:
        logger.warning("Stripe webhook payload is invalid JSON")
        return HttpResponse(status=400)

    # Offload processing to Celery — return 200 immediately
    process_stripe_webhook.delay(payload.decode("utf-8"), signature)
    return HttpResponse(status=200)
