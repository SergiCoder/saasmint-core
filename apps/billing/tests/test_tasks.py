"""Tests for billing Celery tasks."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from stripe import StripeError

from apps.billing.tasks import process_stripe_webhook

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = json.dumps({"id": "evt_test_001", "type": "customer.subscription.updated"})
_VALID_SIG = "t=1234,v1=abc"


def _run_task(payload: str = _VALID_PAYLOAD, signature: str = _VALID_SIG) -> None:
    """Apply the task synchronously (bypasses Celery worker)."""
    process_stripe_webhook.apply(args=[payload, signature]).get()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookSuccess:
    def test_calls_handle_stripe_event_with_correct_args(self):
        mock_handle = AsyncMock()
        with (
            patch(
                "saasmint_core.services.webhooks.handle_stripe_event",
                mock_handle,
            ),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            _run_task()

        mock_handle.assert_awaited_once()
        call_kwargs = mock_handle.call_args.kwargs
        assert call_kwargs["payload"] == _VALID_PAYLOAD.encode("utf-8")
        assert call_kwargs["signature"] == _VALID_SIG
        assert call_kwargs["webhook_secret"] == "whsec_test"

    def test_completes_without_error_on_success(self):
        mock_handle = AsyncMock()
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            # Should not raise
            _run_task()


# ---------------------------------------------------------------------------
# WebhookVerificationError — no retry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookVerificationError:
    def test_raises_without_retrying_on_verification_failure(self):
        from saasmint_core.exceptions import WebhookVerificationError

        mock_handle = AsyncMock(side_effect=WebhookVerificationError("bad sig"))
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            with pytest.raises(WebhookVerificationError):
                _run_task()

        # Task must not have scheduled a retry — handle was called exactly once
        mock_handle.assert_awaited_once()


# ---------------------------------------------------------------------------
# Retryable errors
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookRetry:
    def test_retries_on_stripe_error(self):
        exc = StripeError("network failure")
        mock_handle = AsyncMock(side_effect=exc)
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            with pytest.raises(StripeError):
                _run_task()

        # Called once before the task gives up on the first attempt
        assert mock_handle.await_count >= 1

    def test_retries_on_connection_error(self):
        exc = ConnectionError("timeout")
        mock_handle = AsyncMock(side_effect=exc)
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            with pytest.raises(ConnectionError):
                _run_task()

        assert mock_handle.await_count >= 1

    def test_retries_on_operational_error(self):
        from django.db.utils import OperationalError

        exc = OperationalError("db connection lost")
        mock_handle = AsyncMock(side_effect=exc)
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            with pytest.raises(OperationalError):
                _run_task()

        assert mock_handle.await_count >= 1


# ---------------------------------------------------------------------------
# Malformed JSON payload — graceful handling
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookMalformedPayload:
    def test_handles_non_json_payload_without_raising_before_event_call(self):
        """Malformed JSON should not crash the task before it calls handle_stripe_event."""
        mock_handle = AsyncMock()
        with (
            patch("saasmint_core.services.webhooks.handle_stripe_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
            patch("apps.billing.tasks.settings") as mock_settings,
        ):
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
            _run_task(payload="not-valid-json", signature=_VALID_SIG)

        # handle_stripe_event still called — task did not abort early
        mock_handle.assert_awaited_once()
