"""Tests for apps.marketing.tasks — the Celery wrapper around the email sender."""

from __future__ import annotations

from unittest.mock import patch

from apps.marketing.tasks import send_marketing_inquiry_email_task


class TestSendMarketingInquiryEmailTask:
    """The task is a thin forwarder over ``send_marketing_inquiry_email``.

    It exists so the view can ``.delay()`` without blocking on Resend; the
    real I/O is the email sender, which has its own tests. Here we just pin
    the contract: the task forwards every kwarg through unchanged.
    """

    def test_task_forwards_kwargs_to_email_sender(self):
        with patch("apps.marketing.email.send_marketing_inquiry_email") as mock_send:
            send_marketing_inquiry_email_task.apply(
                kwargs={
                    "to": "ops@saasmint.test",
                    "source": "landing-cta",
                    "sender": "visitor@example.com",
                    "message": "hello",
                }
            ).get()

        mock_send.assert_called_once_with(
            to="ops@saasmint.test",
            source="landing-cta",
            sender="visitor@example.com",
            message="hello",
        )

    def test_task_forwards_empty_message(self):
        # Landing-CTA inquiries arrive with message="" — make sure the task
        # passes the empty string through rather than dropping the kwarg.
        with patch("apps.marketing.email.send_marketing_inquiry_email") as mock_send:
            send_marketing_inquiry_email_task.apply(
                kwargs={
                    "to": "ops@saasmint.test",
                    "source": "landing-cta",
                    "sender": "visitor@example.com",
                    "message": "",
                }
            ).get()

        assert mock_send.call_args.kwargs["message"] == ""

    def test_task_propagates_email_sender_exception(self):
        # If Resend raises, the task must surface it so Celery can retry/log;
        # silently swallowing would lose the inquiry without telemetry.
        with patch("apps.marketing.email.send_marketing_inquiry_email") as mock_send:
            mock_send.side_effect = RuntimeError("resend down")
            result = send_marketing_inquiry_email_task.apply(
                kwargs={
                    "to": "ops@saasmint.test",
                    "source": "landing-cta",
                    "sender": "visitor@example.com",
                    "message": "",
                }
            )

        assert result.failed()
        assert isinstance(result.result, RuntimeError)
