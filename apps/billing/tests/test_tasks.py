"""Tests for billing Celery tasks."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from stripe import StripeError

from apps.billing.tasks import process_stripe_webhook, sync_exchange_rates

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


# ---------------------------------------------------------------------------
# sync_exchange_rates
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSyncExchangeRates:
    def test_creates_exchange_rate_rows(self):
        from apps.billing.models import ExchangeRate

        mock_obj = MagicMock()
        mock_obj.rates = {"eur": 0.91, "gbp": 0.79, "jpy": 149.5}

        with patch("stripe.ExchangeRate.retrieve", return_value=mock_obj):
            sync_exchange_rates.apply().get()

        assert ExchangeRate.objects.filter(currency="eur").exists()
        assert ExchangeRate.objects.filter(currency="gbp").exists()
        assert ExchangeRate.objects.filter(currency="jpy").exists()

    def test_updates_existing_rates_on_second_run(self):
        from decimal import Decimal

        from apps.billing.models import ExchangeRate

        mock_obj_1 = MagicMock()
        mock_obj_1.rates = {"eur": 0.91}
        mock_obj_2 = MagicMock()
        mock_obj_2.rates = {"eur": 0.95}

        with patch("stripe.ExchangeRate.retrieve", return_value=mock_obj_1):
            sync_exchange_rates.apply().get()

        with patch("stripe.ExchangeRate.retrieve", return_value=mock_obj_2):
            sync_exchange_rates.apply().get()

        assert ExchangeRate.objects.count() == 1
        assert ExchangeRate.objects.get(currency="eur").rate == Decimal("0.95")

    def test_handles_stripe_error_gracefully(self):
        from apps.billing.models import ExchangeRate

        with patch("stripe.ExchangeRate.retrieve", side_effect=StripeError("fail")):
            sync_exchange_rates.apply().get()

        assert ExchangeRate.objects.count() == 0

    def test_skips_currencies_missing_from_stripe_response(self):
        """Currencies in SUPPORTED_CURRENCIES but absent from Stripe rates are skipped."""
        from apps.billing.models import ExchangeRate

        # Only return eur — all other supported currencies should be skipped
        mock_obj = MagicMock()
        mock_obj.rates = {"eur": 0.91}

        with patch("stripe.ExchangeRate.retrieve", return_value=mock_obj):
            sync_exchange_rates.apply().get()

        assert ExchangeRate.objects.count() == 1
        assert ExchangeRate.objects.filter(currency="eur").exists()

    def test_usd_never_stored(self):
        """USD is skipped even if present in Stripe rates (it's the base currency)."""
        from apps.billing.models import ExchangeRate

        mock_obj = MagicMock()
        mock_obj.rates = {"usd": 1.0, "eur": 0.91}

        with patch("stripe.ExchangeRate.retrieve", return_value=mock_obj):
            sync_exchange_rates.apply().get()

        assert not ExchangeRate.objects.filter(currency="usd").exists()
        assert ExchangeRate.objects.filter(currency="eur").exists()
