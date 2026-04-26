"""Celery tasks for marketing operations."""

from __future__ import annotations

from config.celery import app


@app.task  # type: ignore[untyped-decorator]  # celery has no stubs
def send_marketing_inquiry_email_task(
    *, to: str, source: str, sender: str, message: str
) -> None:
    """Forward a marketing inquiry to the configured admin inbox (async-safe)."""
    from apps.marketing.email import send_marketing_inquiry_email

    send_marketing_inquiry_email(to=to, source=source, sender=sender, message=message)
