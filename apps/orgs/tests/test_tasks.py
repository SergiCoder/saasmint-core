"""Tests for apps.orgs.tasks — Stripe sub-cancel task idempotency."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import stripe

from apps.orgs.tasks import cancel_stripe_subs_task


class TestCancelStripeSubsTaskIdempotency:
    """The task can be called more than once for the same sub_id without
    failing (DELETE-then-webhook race, Celery retry after partial success)."""

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_uses_prorate_false(self, mock_cancel):
        cancel_stripe_subs_task(["sub_x"], "org_x")
        mock_cancel.assert_called_once_with("sub_x", prorate=False)

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_swallows_resource_missing(self, mock_cancel):
        mock_cancel.side_effect = stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
            "No such subscription", param="id", code="resource_missing"
        )
        cancel_stripe_subs_task(["sub_already_gone"], "org_xyz")
        mock_cancel.assert_called_once()

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_propagates_other_invalid_request_errors(self, mock_cancel):
        mock_cancel.side_effect = stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
            "Bad request", param="id", code="parameter_unknown"
        )
        with pytest.raises(stripe.InvalidRequestError):
            cancel_stripe_subs_task(["sub_bad"], "org_xyz")

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_propagates_non_invalid_request_stripe_errors(self, mock_cancel):
        """The narrowed except clause must let APIConnectionError, RateLimitError,
        etc. propagate so Celery records the failure for retry/inspection."""
        mock_cancel.side_effect = stripe.APIConnectionError("network down")  # type: ignore[no-untyped-call]
        with pytest.raises(stripe.APIConnectionError):
            cancel_stripe_subs_task(["sub_net"], "org_xyz")

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_processes_each_id_independently(self, mock_cancel):
        mock_cancel.side_effect = [
            stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
                "gone", param="id", code="resource_missing"
            ),
            None,
        ]
        cancel_stripe_subs_task(["sub_gone", "sub_live"], "user:abc")
        assert mock_cancel.call_count == 2

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_continues_loop_after_transient_then_raises(self, mock_cancel):
        """A transient Stripe error on one sub must not skip the remaining ones;
        the failure is still re-raised at the end so Celery records it."""
        mock_cancel.side_effect = [
            stripe.APIConnectionError("boom"),  # type: ignore[no-untyped-call]
            None,
            None,
        ]
        with pytest.raises(stripe.APIConnectionError):
            cancel_stripe_subs_task(["sub_a", "sub_b", "sub_c"], "user:abc")
        assert mock_cancel.call_count == 3

    @patch("apps.orgs.tasks.stripe.Subscription.cancel")
    def test_continues_loop_after_invalid_request_then_raises(self, mock_cancel):
        mock_cancel.side_effect = [
            stripe.InvalidRequestError(  # type: ignore[no-untyped-call]
                "bad", param="id", code="parameter_unknown"
            ),
            None,
        ]
        with pytest.raises(stripe.InvalidRequestError):
            cancel_stripe_subs_task(["sub_bad", "sub_ok"], "user:abc")
        assert mock_cancel.call_count == 2
