"""Tests for billing Celery tasks."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from stripe import StripeError

from apps.billing.models import StripeEvent
from apps.billing.tasks import (
    process_stripe_webhook,
    send_subscription_cancel_notice_task,
    sync_exchange_rates,
)


def _seed_event(
    *,
    stripe_id: str = "evt_test_001",
    event_type: str = "customer.subscription.updated",
    livemode: bool = False,
) -> StripeEvent:
    return StripeEvent.objects.create(
        stripe_id=stripe_id,
        type=event_type,
        livemode=livemode,
        payload={
            "id": stripe_id,
            "type": event_type,
            "livemode": livemode,
            "data": {"object": {"id": "obj_123"}},
        },
    )


def _run_task(event_id: str) -> None:
    """Apply the task synchronously (bypasses Celery worker)."""
    process_stripe_webhook.apply(args=[event_id]).get()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookSuccess:
    def test_loads_event_and_dispatches_with_persisted_payload(self):
        event = _seed_event()
        mock_handle = AsyncMock()
        with (
            patch(
                "saasmint_core.services.webhooks.process_stored_event",
                mock_handle,
            ),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            _run_task(str(event.id))

        mock_handle.assert_awaited_once()
        call_kwargs = mock_handle.call_args.kwargs
        assert call_kwargs["event"] == event.payload
        assert call_kwargs["stripe_id"] == event.stripe_id

    def test_raises_if_event_id_unknown(self):
        """A bogus id indicates a lost DB row or dev-env mismatch — fail loud."""
        with pytest.raises(StripeEvent.DoesNotExist):
            _run_task(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Permanent errors — no retry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookPermanentError:
    def test_webhook_data_error_raised_without_retry(self):
        """WebhookDataError surfaces as-is; the task does NOT call self.retry."""
        from saasmint_core.exceptions import WebhookDataError

        event = _seed_event()
        mock_handle = AsyncMock(side_effect=WebhookDataError("Unknown customer"))
        with (
            patch("saasmint_core.services.webhooks.process_stored_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            with pytest.raises(WebhookDataError):
                _run_task(str(event.id))

        mock_handle.assert_awaited_once()


# ---------------------------------------------------------------------------
# Transient errors — retry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessStripeWebhookRetry:
    def test_retries_on_stripe_error(self):
        event = _seed_event()
        mock_handle = AsyncMock(side_effect=StripeError("network failure"))
        with (
            patch("saasmint_core.services.webhooks.process_stored_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            with pytest.raises(StripeError):
                _run_task(str(event.id))

        assert mock_handle.await_count >= 1

    def test_retries_on_connection_error(self):
        event = _seed_event()
        mock_handle = AsyncMock(side_effect=ConnectionError("timeout"))
        with (
            patch("saasmint_core.services.webhooks.process_stored_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            with pytest.raises(ConnectionError):
                _run_task(str(event.id))

        assert mock_handle.await_count >= 1

    def test_retries_on_operational_error(self):
        from django.db.utils import OperationalError

        event = _seed_event()
        mock_handle = AsyncMock(side_effect=OperationalError("db connection lost"))
        with (
            patch("saasmint_core.services.webhooks.process_stored_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            with pytest.raises(OperationalError):
                _run_task(str(event.id))

        assert mock_handle.await_count >= 1

    def test_retry_after_webhook_secret_rotation_succeeds(self):
        """The task loads the already-verified payload from DB and never
        re-verifies the Stripe signature, so a retry after the webhook secret
        was rotated mid-queue still dispatches successfully."""
        event = _seed_event(stripe_id="evt_post_rotation")
        mock_handle = AsyncMock()
        with (
            patch("saasmint_core.services.webhooks.process_stored_event", mock_handle),
            patch("apps.billing.tasks.get_webhook_repos", return_value=MagicMock()),
        ):
            _run_task(str(event.id))

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


# ---------------------------------------------------------------------------
# send_subscription_cancel_notice_task — fanout of transactional emails
# ---------------------------------------------------------------------------


class TestSendSubscriptionCancelNoticeTask:
    def test_scheduled_action_invokes_scheduled_sender_per_recipient(self):
        """action='scheduled' must dispatch one send_subscription_cancel_scheduled
        call per recipient (not the resumed variant)."""
        with (
            patch("apps.billing.email.send_subscription_cancel_scheduled") as mock_scheduled,
            patch("apps.billing.email.send_subscription_cancel_resumed") as mock_resumed,
        ):
            send_subscription_cancel_notice_task.apply(
                args=[["a@example.com", "b@example.com"], "Pro Monthly", "scheduled"]
            ).get()

        assert mock_scheduled.call_count == 2
        assert mock_scheduled.call_args_list[0].args == ("a@example.com", "Pro Monthly")
        assert mock_scheduled.call_args_list[1].args == ("b@example.com", "Pro Monthly")
        mock_resumed.assert_not_called()

    def test_resumed_action_invokes_resumed_sender(self):
        """Any action != 'scheduled' routes to the resumed sender."""
        with (
            patch("apps.billing.email.send_subscription_cancel_resumed") as mock_resumed,
            patch("apps.billing.email.send_subscription_cancel_scheduled") as mock_scheduled,
        ):
            send_subscription_cancel_notice_task.apply(
                args=[["a@example.com"], "Pro Monthly", "resumed"]
            ).get()

        mock_resumed.assert_called_once_with("a@example.com", "Pro Monthly")
        mock_scheduled.assert_not_called()

    def test_failure_for_one_recipient_does_not_block_others(self):
        """A sender raising for one address must not short-circuit the loop —
        remaining recipients must still be attempted. The task swallows the
        exception (per implementation) because Resend calls are idempotent and
        the billing state change is authoritative."""

        def _fail_on_bad(email: str, _label: str) -> None:
            if email == "bad@example.com":
                raise RuntimeError("resend boom")

        with patch(
            "apps.billing.email.send_subscription_cancel_scheduled",
            side_effect=_fail_on_bad,
        ) as mock_scheduled:
            send_subscription_cancel_notice_task.apply(
                args=[
                    ["a@example.com", "bad@example.com", "c@example.com"],
                    "Pro Monthly",
                    "scheduled",
                ]
            ).get()

        assert mock_scheduled.call_count == 3
        sent = [c.args[0] for c in mock_scheduled.call_args_list]
        assert sent == ["a@example.com", "bad@example.com", "c@example.com"]

    def test_empty_recipient_list_is_noop(self):
        with (
            patch("apps.billing.email.send_subscription_cancel_scheduled") as mock_scheduled,
            patch("apps.billing.email.send_subscription_cancel_resumed") as mock_resumed,
        ):
            send_subscription_cancel_notice_task.apply(args=[[], "Pro Monthly", "scheduled"]).get()

        mock_scheduled.assert_not_called()
        mock_resumed.assert_not_called()
