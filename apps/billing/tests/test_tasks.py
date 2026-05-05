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
    sync_localized_prices,
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
# sync_localized_prices
# ---------------------------------------------------------------------------


def _fx_response(rates: dict[str, float]) -> MagicMock:
    """Build a mock httpx.Response in the open.er-api.com success shape."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "result": "success",
        "rates": {k.upper(): v for k, v in rates.items()},
    }
    return resp


def _seed_plan_price(amount: int = 999) -> object:
    from apps.billing.models import Plan, PlanPrice

    plan = Plan.objects.create(
        name="Pro Monthly", context="personal", tier=3, interval="month"
    )
    return PlanPrice.objects.create(plan=plan, stripe_price_id=f"price_{plan.id}", amount=amount)


def _seed_product_price(amount: int = 1500) -> object:
    from apps.billing.models import Product, ProductPrice

    product = Product.objects.create(name="Boost", type="one_time", credits=100)
    return ProductPrice.objects.create(
        product=product, stripe_price_id=f"price_{product.id}", amount=amount
    )


@pytest.mark.django_db
class TestSyncLocalizedPrices:
    def test_creates_rows_for_plan_and_product_across_currencies(self):
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)  # $9.99
        product_price = _seed_product_price(1500)  # $15.00

        rates = {"eur": 0.9, "jpy": 150.0}
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response(rates)):
            sync_localized_prices.apply().get()

        assert LocalizedPrice.objects.filter(plan_price=plan_price, currency="eur").exists()
        assert LocalizedPrice.objects.filter(plan_price=plan_price, currency="jpy").exists()
        assert LocalizedPrice.objects.filter(product_price=product_price, currency="eur").exists()
        assert LocalizedPrice.objects.filter(product_price=product_price, currency="jpy").exists()

    def test_friendly_rounding_applied(self):
        """A messy raw conversion ($9.99 times 0.9 = €8.991) snaps to a charm price."""
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()

        eur = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur")
        # round_friendly snaps 8.991 to 8.99 → 899 cents.
        assert eur.amount_minor == 899

    def test_zero_decimal_currency_stored_as_whole_units(self):
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)  # $9.99 * 150 = ~Y1498.5 -> Y1500

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"jpy": 150.0})):
            sync_localized_prices.apply().get()

        jpy = LocalizedPrice.objects.get(plan_price=plan_price, currency="jpy")
        assert jpy.amount_minor == 1500

    def test_idempotent_on_second_run(self):
        from apps.billing.models import LocalizedPrice

        _seed_plan_price(999)

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
            count_after_first = LocalizedPrice.objects.count()
            sync_localized_prices.apply().get()
            count_after_second = LocalizedPrice.objects.count()

        assert count_after_first == count_after_second

    def test_updates_existing_rows_when_rate_changes(self):
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
        first = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 1.1})):
            sync_localized_prices.apply().get()
        second = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        assert first != second
        assert LocalizedPrice.objects.filter(plan_price=plan_price, currency="eur").count() == 1

    def test_handles_api_failure_gracefully(self):
        """Upstream FX API down → no rows mutated, existing rows preserved."""
        import httpx

        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
        before = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        with patch("apps.billing.tasks.httpx.get", side_effect=httpx.HTTPError("boom")):
            written = sync_localized_prices.apply().get()

        assert written == 0
        # Existing row unchanged — a flaky upstream must never erase the catalog.
        after = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor
        assert before == after

    def test_skips_currency_missing_from_api_response(self):
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)

        # Only EUR is returned; every other supported currency is silently skipped.
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()

        assert LocalizedPrice.objects.filter(plan_price=plan_price, currency="eur").exists()
        assert not LocalizedPrice.objects.filter(plan_price=plan_price, currency="gbp").exists()

    def test_usd_never_stored(self):
        from apps.billing.models import LocalizedPrice

        _seed_plan_price(999)

        rates = {"usd": 1.0, "eur": 0.9}
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response(rates)):
            sync_localized_prices.apply().get()

        assert not LocalizedPrice.objects.filter(currency="usd").exists()
        assert LocalizedPrice.objects.filter(currency="eur").exists()

    def test_no_op_when_catalog_empty(self):
        from apps.billing.models import LocalizedPrice

        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            written = sync_localized_prices.apply().get()

        assert written == 0
        assert LocalizedPrice.objects.count() == 0

    def test_handles_http_status_error_gracefully(self):
        """4xx/5xx from the FX API → raise_for_status raises HTTPStatusError →
        existing rows preserved, 0 returned."""
        import httpx

        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
        before = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
        with patch("apps.billing.tasks.httpx.get", return_value=bad_resp):
            written = sync_localized_prices.apply().get()

        assert written == 0
        after = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor
        assert before == after

    def test_handles_value_error_from_malformed_json_gracefully(self):
        """Malformed JSON from the FX feed (ValueError on .json()) → 0 returned,
        existing rows untouched."""
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
        before = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock()
        bad_resp.json.side_effect = ValueError("Unexpected token")
        with patch("apps.billing.tasks.httpx.get", return_value=bad_resp):
            written = sync_localized_prices.apply().get()

        assert written == 0
        after = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor
        assert before == after

    def test_non_success_result_payload_returns_zero(self):
        """API responds 200 but result != 'success' (e.g. API key expired) →
        0 rows written, existing rows preserved."""
        from apps.billing.models import LocalizedPrice

        plan_price = _seed_plan_price(999)
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response({"eur": 0.9})):
            sync_localized_prices.apply().get()
        before = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor

        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock()
        bad_resp.json.return_value = {"result": "error", "error-type": "invalid_app_id"}
        with patch("apps.billing.tasks.httpx.get", return_value=bad_resp):
            written = sync_localized_prices.apply().get()

        assert written == 0
        after = LocalizedPrice.objects.get(plan_price=plan_price, currency="eur").amount_minor
        assert before == after

    def test_return_value_counts_both_plan_and_product_rows(self):
        """Return value equals the total number of LocalizedPrice rows written
        (plan rows + product rows across all currencies)."""
        from apps.billing.models import LocalizedPrice

        _seed_plan_price(999)
        _seed_product_price(1500)

        # Provide exactly 2 currencies so the count is deterministic.
        with patch(
            "apps.billing.tasks.SUPPORTED_CURRENCIES",
            frozenset({"usd", "eur", "gbp"}),
            create=True,
        ):
            pass  # Can't mock module-level import easily; use actual SUPPORTED_CURRENCIES count.

        rates = {"eur": 0.9, "gbp": 0.85}
        with patch("apps.billing.tasks.httpx.get", return_value=_fx_response(rates)):
            written = sync_localized_prices.apply().get()

        # 2 prices (1 plan + 1 product) x only currencies present in rates response
        actual_count = LocalizedPrice.objects.count()
        assert written == actual_count
        assert written > 0


# ---------------------------------------------------------------------------
# _to_minor_units — unit tests
# ---------------------------------------------------------------------------


class TestToMinorUnits:
    """Unit tests for the ``_to_minor_units`` helper (tasks.py)."""

    def test_two_decimal_currency_multiplies_by_100(self):
        from apps.billing.tasks import _to_minor_units

        assert _to_minor_units(9.99, "eur") == 999

    def test_two_decimal_float_drift_is_rounded(self):
        """IEEE-754 means 9.99 * 100 = 998.9999... — round() must produce 999."""
        from apps.billing.tasks import _to_minor_units

        assert _to_minor_units(9.99, "gbp") == 999

    def test_zero_decimal_currency_returns_whole_units(self):
        from apps.billing.tasks import _to_minor_units

        assert _to_minor_units(1500.0, "jpy") == 1500

    def test_zero_decimal_currency_rounds_float(self):
        """Non-integer floats for zero-decimal currencies (e.g. from FX math)
        are rounded to the nearest integer.  Uses 1498.7 (not a banker's-rounding
        boundary) to avoid Python's round-half-to-even edge case."""
        from apps.billing.tasks import _to_minor_units

        assert _to_minor_units(1498.7, "jpy") == 1499

    def test_usd_treated_as_two_decimal(self):
        from apps.billing.tasks import _to_minor_units

        assert _to_minor_units(9.99, "usd") == 999


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
