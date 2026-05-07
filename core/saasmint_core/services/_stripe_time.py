"""Shared Stripe timestamp helpers.

Stripe sends Unix-epoch integers (sometimes ``None`` when a field is
unset). Both ``services.billing`` and ``services.webhooks`` need to
convert those values to timezone-aware ``datetime`` rows for the
local mirror, so the helper lives here in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime


def ts_to_dt(value: int | float | None) -> datetime | None:
    """Stripe-style Unix timestamp → aware UTC datetime, ``None`` passthrough."""
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)
