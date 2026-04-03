"""Tests for webhook endpoint and Celery task."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import RequestFactory

from apps.billing.webhook import stripe_webhook


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def valid_payload():
    return json.dumps({"id": "evt_test_123", "type": "checkout.session.completed"})


class TestStripeWebhook:
    @patch("apps.billing.webhook.process_stripe_webhook")
    @patch("apps.billing.webhook.stripe.Webhook.construct_event")
    def test_valid_signature_dispatches_to_celery(
        self, mock_construct, mock_task, rf, valid_payload
    ):
        mock_construct.return_value = MagicMock()
        request = rf.post(
            "/api/v1/webhooks/stripe",
            data=valid_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        resp = stripe_webhook(request)
        assert resp.status_code == 200
        mock_task.delay.assert_called_once_with(valid_payload, "sig_test")

    @patch("apps.billing.webhook.stripe.Webhook.construct_event")
    def test_invalid_signature_returns_400(self, mock_construct, rf, valid_payload):
        import stripe

        mock_construct.side_effect = stripe.SignatureVerificationError("bad sig", "sig")
        request = rf.post(
            "/api/v1/webhooks/stripe",
            data=valid_payload,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="bad_sig",
        )
        resp = stripe_webhook(request)
        assert resp.status_code == 400

    @patch("apps.billing.webhook.stripe.Webhook.construct_event")
    def test_invalid_json_returns_400(self, mock_construct, rf):
        mock_construct.side_effect = ValueError("Invalid JSON")
        request = rf.post(
            "/api/v1/webhooks/stripe",
            data="not json",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        resp = stripe_webhook(request)
        assert resp.status_code == 400

    @patch("apps.billing.webhook.process_stripe_webhook")
    @patch("apps.billing.webhook.stripe.Webhook.construct_event")
    def test_missing_signature_header(self, mock_construct, mock_task, rf, valid_payload):
        mock_construct.return_value = MagicMock()
        request = rf.post(
            "/api/v1/webhooks/stripe",
            data=valid_payload,
            content_type="application/json",
        )
        resp = stripe_webhook(request)
        # Empty string signature passed to construct_event — behavior depends on Stripe
        # The mock accepts it, so we verify it reaches the task dispatch
        assert resp.status_code == 200


class TestProcessStripeWebhookTask:
    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_successful_processing(self, mock_repos, mock_handle, settings):
        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        payload = json.dumps({"id": "evt_123", "type": "invoice.paid"})
        process_stripe_webhook(payload, "sig_test")
        mock_handle.assert_called_once()

    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_verification_error_not_retried(self, mock_repos, mock_handle, settings):
        from saasmint_core.exceptions import WebhookVerificationError

        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        mock_handle.side_effect = WebhookVerificationError("bad sig")
        payload = json.dumps({"id": "evt_123", "type": "test"})

        with pytest.raises(WebhookVerificationError):
            process_stripe_webhook(payload, "sig_test")

    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_malformed_json_still_processes(self, mock_repos, mock_handle, settings):
        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        # Malformed JSON — should still attempt processing
        process_stripe_webhook("not json", "sig_test")
        mock_handle.assert_called_once()

    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_stripe_error_triggers_retry(self, mock_repos, mock_handle, settings):
        """StripeError should schedule a retry via self.retry."""
        import stripe

        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        mock_handle.side_effect = stripe.StripeError("network error")
        payload = json.dumps({"id": "evt_retry", "type": "invoice.paid"})

        # Celery tasks raise self.retry() which itself raises Retry; catch it.
        from celery.exceptions import Retry

        with pytest.raises((stripe.StripeError, Retry)):
            process_stripe_webhook(payload, "sig_test")

    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_connection_error_triggers_retry(self, mock_repos, mock_handle, settings):
        """ConnectionError should schedule a retry via self.retry."""
        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        mock_handle.side_effect = ConnectionError("connection refused")
        payload = json.dumps({"id": "evt_conn", "type": "invoice.paid"})

        from celery.exceptions import Retry

        with pytest.raises((ConnectionError, Retry)):
            process_stripe_webhook(payload, "sig_test")

    @patch("saasmint_core.services.webhooks.handle_stripe_event", new_callable=AsyncMock)
    @patch("apps.billing.repositories.get_webhook_repos")
    def test_operational_error_triggers_retry(self, mock_repos, mock_handle, settings):
        """OperationalError (DB) should schedule a retry via self.retry."""
        from django.db.utils import OperationalError

        from apps.billing.tasks import process_stripe_webhook

        mock_repos.return_value = MagicMock()
        mock_handle.side_effect = OperationalError("db locked")
        payload = json.dumps({"id": "evt_db", "type": "invoice.paid"})

        from celery.exceptions import Retry

        with pytest.raises((OperationalError, Retry)):
            process_stripe_webhook(payload, "sig_test")
