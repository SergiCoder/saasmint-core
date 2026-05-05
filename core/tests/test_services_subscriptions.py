"""Tests for services/subscriptions.py — all branches covered."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from saasmint_core.services.subscriptions import _safe_get, change_plan, update_seat_count
from tests.conftest import InMemorySubscriptionRepository, make_subscription

# ── _safe_get ─────────────────────────────────────────────────────────────────


def test_safe_get_returns_value_from_dict() -> None:
    assert _safe_get({"key": "val"}, "key") == "val"


def test_safe_get_missing_key_returns_none() -> None:
    assert _safe_get({"other": 1}, "key") is None


def test_safe_get_none_obj_returns_none() -> None:
    assert _safe_get(None, "key") is None


def test_safe_get_returns_none_for_non_subscriptable() -> None:
    """Objects that don't support __getitem__ (e.g. plain int) must return
    None instead of raising TypeError."""
    assert _safe_get(42, "key") is None

# ── change_plan ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_change_plan_with_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_abc"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=True,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_abc", "price": "price_new", "quantity": 1}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_change_plan_without_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_def"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=False,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_def", "price": "price_new", "quantity": 1}],
        proration_behavior="none",
    )


@pytest.mark.anyio
async def test_change_plan_preserves_current_seat_count_when_quantity_omitted() -> None:
    """Regression: when the caller doesn't pass ``quantity``, the current
    seat count from the Stripe item must be carried into the modify call.
    Otherwise Stripe defaults the item to 1 and silently wipes seats on a
    plan switch."""
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_keep", "quantity": 7}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=True,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_keep", "price": "price_new", "quantity": 7}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_change_plan_with_quantity() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_combo"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=True,
            quantity=5,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_combo", "price": "price_new", "quantity": 5}],
        proration_behavior="create_prorations",
    )


@pytest.mark.anyio
async def test_change_plan_with_quantity_no_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_nopro"}]}}[k]
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_abc",
            new_stripe_price_id="price_new",
            prorate=False,
            quantity=3,
        )

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_nopro", "price": "price_new", "quantity": 3}],
        proration_behavior="none",
    )


# ── update_seat_count ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_update_seat_count_increase_prorates() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_seat", "quantity": 3}]}}[k]
    )
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc", seat_limit=3)
    await repo.save(active)

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(active=active, quantity=5, subscription_repo=repo)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_seat", "quantity": 5}],
        proration_behavior="create_prorations",
    )
    # Optimistic mirror: local row reflects the new seat count before the
    # webhook lands.
    stored = await repo.get_by_id(active.id)
    assert stored is not None
    assert stored.seat_limit == 5


@pytest.mark.anyio
async def test_update_seat_count_decrease_no_proration() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_seat", "quantity": 5}]}}[k]
    )
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc", seat_limit=5)
    await repo.save(active)

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(active=active, quantity=3, subscription_repo=repo)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_seat", "quantity": 3}],
        proration_behavior="none",
    )
    stored = await repo.get_by_id(active.id)
    assert stored is not None
    assert stored.seat_limit == 3


@pytest.mark.anyio
async def test_update_seat_count_minimum_valid() -> None:
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_min", "quantity": 1}]}}[k]
    )
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc", seat_limit=1)
    await repo.save(active)

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify") as mock_modify,
    ):
        await update_seat_count(active=active, quantity=1, subscription_repo=repo)

    mock_modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_min", "quantity": 1}],
        proration_behavior="none",
    )


@pytest.mark.anyio
async def test_update_seat_count_no_save_when_quantity_unchanged() -> None:
    """Skip the optimistic save when the new quantity already matches the
    local row — avoids a pointless DB write on no-op submissions."""
    mock_sub = MagicMock()
    mock_sub.__getitem__ = MagicMock(
        side_effect=lambda k: {"items": {"data": [{"id": "si_same", "quantity": 4}]}}[k]
    )
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc", seat_limit=4)
    await repo.save(active)
    save_spy = MagicMock(wraps=repo.save)
    repo.save = save_spy  # type: ignore[method-assign]

    with (
        patch("stripe.Subscription.retrieve", return_value=mock_sub),
        patch("stripe.Subscription.modify"),
    ):
        await update_seat_count(active=active, quantity=4, subscription_repo=repo)

    save_spy.assert_not_called()


@pytest.mark.anyio
async def test_update_seat_count_zero_raises() -> None:
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc")
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(active=active, quantity=0, subscription_repo=repo)


@pytest.mark.anyio
async def test_update_seat_count_negative_raises() -> None:
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id="sub_abc")
    with pytest.raises(ValueError, match="at least 1"):
        await update_seat_count(active=active, quantity=-3, subscription_repo=repo)


@pytest.mark.anyio
async def test_update_seat_count_missing_stripe_id_raises() -> None:
    repo = InMemorySubscriptionRepository()
    active = make_subscription(stripe_id=None)
    with pytest.raises(ValueError, match="no stripe_id"):
        await update_seat_count(active=active, quantity=2, subscription_repo=repo)


# ── change_plan: defer-on-downgrade ──────────────────────────────────────────


def _stripe_sub_dict(
    *,
    sub_id: str = "sub_dg",
    item_id: str = "si_dg",
    price_id: str = "price_pro",
    unit_amount: int = 2000,
    quantity: int = 1,
    currency: str = "usd",
    period_start: int = 1_700_000_000,
    period_end: int = 1_702_592_000,
) -> dict[str, object]:
    """Stripe-shaped subscription dict for change_plan tests.

    Carries ``unit_amount`` and ``current_period_start/end`` on the first
    item — the fields ``change_plan`` needs to decide whether to defer and
    to build the SubscriptionSchedule phases.
    """
    return {
        "id": sub_id,
        "items": {
            "data": [
                {
                    "id": item_id,
                    "price": {
                        "id": price_id,
                        "unit_amount": unit_amount,
                        "currency": currency,
                    },
                    "quantity": quantity,
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                }
            ]
        },
    }


def _retrieved_price(*, currency: str = "usd") -> MagicMock:
    """Build a ``stripe.Price.retrieve`` return value carrying *currency*.

    ``_schedule_downgrade_at_period_end`` calls ``Price.retrieve`` for the
    incoming new price ID to assert it matches the subscription's currency
    before building the schedule phases — without this mock the call hits
    the live Stripe API in tests."""
    p = MagicMock()
    p.currency = currency
    return p


@pytest.mark.anyio
async def test_change_plan_downgrade_defers_via_schedule() -> None:
    """``new_price_amount < current unit_amount`` → no Subscription.modify;
    a two-phase SubscriptionSchedule is created instead. Phase 0 keeps the
    current price until period end; phase 1 starts at period end on the
    new price. Both phases preserve the current quantity."""
    sub = _stripe_sub_dict(
        unit_amount=2000, quantity=3, period_start=1_700_000_000, period_end=1_702_592_000
    )

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.Price.retrieve", return_value=_retrieved_price()),
        patch(
            "stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_new"}
        ) as mock_create,
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
        )

    assert result == "scheduled_for_period_end"
    mock_modify.assert_not_called()
    mock_create.assert_called_once_with(from_subscription="sub_dg")
    args, kwargs = mock_sched_modify.call_args
    assert args[0] == "sub_sched_new"
    assert kwargs["end_behavior"] == "release"
    phases = kwargs["phases"]
    assert len(phases) == 2
    # Phase 0: stay on current price until period end, with current seat count.
    assert phases[0]["items"][0]["price"] == "price_pro"
    assert phases[0]["items"][0]["quantity"] == 3
    assert phases[0]["end_date"] == 1_702_592_000
    # Phase 1: switch to new price at the same instant; seats preserved.
    assert phases[1]["items"][0]["price"] == "price_basic"
    assert phases[1]["items"][0]["quantity"] == 3
    assert phases[1]["start_date"] == 1_702_592_000


@pytest.mark.anyio
async def test_change_plan_upgrade_applies_immediately() -> None:
    """``new_price_amount > current unit_amount`` → Subscription.modify (no
    schedule). Customer pays the prorated diff now."""
    sub = _stripe_sub_dict(unit_amount=999)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_pro",
            new_price_amount=2000,
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_same_amount_applies_immediately() -> None:
    """Same-amount switch (e.g. tier swap with identical price) is not a
    downgrade — no defer, immediate modify."""
    sub = _stripe_sub_dict(unit_amount=1500)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_other",
            new_price_amount=1500,
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_without_amount_uses_legacy_immediate_path() -> None:
    """Backwards compat: callers that don't pass ``new_price_amount`` get
    immediate-modify behavior regardless of whether the swap would
    otherwise be a downgrade. The Stripe portal deep-link callers + any
    legacy code path are not forced into schedule creation."""
    sub = _stripe_sub_dict(unit_amount=2000)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
        )

    assert result == "applied_now"
    mock_create.assert_not_called()
    mock_modify.assert_called_once()


@pytest.mark.anyio
async def test_change_plan_downgrade_quantity_override_wins() -> None:
    """An explicit ``quantity`` argument overrides the current seat count
    on both phases — a deferred downgrade can also adjust seats."""
    sub = _stripe_sub_dict(unit_amount=2000, quantity=5)

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch("stripe.Price.retrieve", return_value=_retrieved_price()),
        patch(
            "stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_q"}
        ),
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
            quantity=2,
        )

    phases = mock_sched_modify.call_args.kwargs["phases"]
    assert phases[0]["items"][0]["quantity"] == 2
    assert phases[1]["items"][0]["quantity"] == 2


@pytest.mark.anyio
async def test_change_plan_downgrade_missing_period_end_raises() -> None:
    """``_schedule_downgrade_at_period_end`` reads ``current_period_end`` from
    the first item. When the field is absent the function raises ``ValueError``
    rather than silently passing ``None`` to Stripe, which would accept it and
    produce an undefined schedule."""
    sub = {
        "id": "sub_no_end",
        "items": {
            "data": [
                {
                    "id": "si_no_end",
                    "price": {"id": "price_pro", "unit_amount": 2000},
                    "quantity": 1,
                    # current_period_end deliberately omitted
                }
            ]
        },
    }

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch("stripe.SubscriptionSchedule.create"),
        pytest.raises(ValueError, match="current_period_end"),
    ):
        await change_plan(
            stripe_subscription_id="sub_no_end",
            new_stripe_price_id="price_basic",
            new_price_amount=999,  # triggers downgrade path
        )


@pytest.mark.anyio
async def test_change_plan_downgrade_missing_period_start_raises() -> None:
    """``_schedule_downgrade_at_period_end`` also validates
    ``current_period_start``. A missing start timestamp must raise rather than
    producing a malformed schedule."""
    sub = {
        "id": "sub_no_start",
        "items": {
            "data": [
                {
                    "id": "si_no_start",
                    "price": {"id": "price_pro", "unit_amount": 2000},
                    "quantity": 1,
                    "current_period_end": 1_702_592_000,
                    # current_period_start deliberately omitted
                }
            ]
        },
    }

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch("stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_ns"}),
        pytest.raises(ValueError, match="current_period_start"),
    ):
        await change_plan(
            stripe_subscription_id="sub_no_start",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
        )


@pytest.mark.anyio
async def test_change_plan_downgrade_reuses_existing_schedule() -> None:
    """When the subscription is already managed by a SubscriptionSchedule
    (``sub["schedule"]`` is set), ``_schedule_downgrade_at_period_end`` must
    skip ``SubscriptionSchedule.create`` and go straight to
    ``SubscriptionSchedule.modify`` on the existing schedule id.
    Stripe rejects a second ``create(from_subscription=...)`` call when the
    sub is already pinned by a schedule."""
    sub = _stripe_sub_dict(
        unit_amount=2000,
        quantity=3,
        period_start=1_700_000_000,
        period_end=1_702_592_000,
    )
    # Inject the existing schedule id onto the sub dict so _safe_get picks it up.
    sub["schedule"] = "sub_sched_existing"

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch("stripe.Price.retrieve", return_value=_retrieved_price()),
        patch("stripe.SubscriptionSchedule.create") as mock_create,
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_dg",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
        )

    assert result == "scheduled_for_period_end"
    # Must NOT create a new schedule — the existing one is reused.
    mock_create.assert_not_called()
    # Must modify the existing schedule id.
    args, kwargs = mock_sched_modify.call_args
    assert args[0] == "sub_sched_existing"
    assert kwargs["end_behavior"] == "release"


@pytest.mark.anyio
async def test_change_plan_upgrade_with_existing_schedule_releases_schedule_first() -> None:
    """When the sub is already owned by a SubscriptionSchedule (pinned by a
    previous deferred downgrade), an upgrade must release the schedule before
    calling ``Subscription.modify`` — Stripe rejects modify on a scheduled sub.
    After release, the sub is re-fetched and the modify proceeds normally."""
    sub_before_release = _stripe_sub_dict(
        sub_id="sub_upgrade_sched",
        item_id="si_before",
        unit_amount=999,  # current price is 999 → upgrade to 2000
        quantity=2,
        period_start=1_700_000_000,
        period_end=1_702_592_000,
    )
    sub_before_release["schedule"] = "sub_sched_pinned"

    # After release, Stripe returns the sub without the schedule field.
    sub_after_release = _stripe_sub_dict(
        sub_id="sub_upgrade_sched",
        item_id="si_after",
        unit_amount=999,
        quantity=2,
    )

    retrieve_call_count = 0

    def side_effect_retrieve(sub_id: str) -> object:
        nonlocal retrieve_call_count
        retrieve_call_count += 1
        return sub_before_release if retrieve_call_count == 1 else sub_after_release

    with (
        patch("stripe.Subscription.retrieve", side_effect=side_effect_retrieve),
        patch("stripe.SubscriptionSchedule.release") as mock_release,
        patch("stripe.Subscription.modify") as mock_modify,
        patch("stripe.SubscriptionSchedule.create") as mock_create,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_upgrade_sched",
            new_stripe_price_id="price_pro",
            new_price_amount=2000,  # upgrade
        )

    assert result == "applied_now"
    # Must NOT have created a new schedule.
    mock_create.assert_not_called()
    # Must have released the pinning schedule before modifying.
    mock_release.assert_called_once_with("sub_sched_pinned")
    # Modify must have been called once with the item from the re-fetched sub.
    mock_modify.assert_called_once()
    modify_items = mock_modify.call_args[1]["items"]
    assert modify_items[0]["id"] == "si_after"


@pytest.mark.anyio
async def test_change_plan_downgrade_period_fallback_from_subscription_level() -> None:
    """``_read_period_field`` reads period timestamps from the item first,
    then falls back to the subscription level. Verify the fallback path:
    item has no ``current_period_start/end``, but the sub object does."""
    # Period fields absent from the item but present at the subscription level.
    sub = {
        "id": "sub_fallback",
        "schedule": None,
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_702_592_000,
        "items": {
            "data": [
                {
                    "id": "si_fallback",
                    "price": {"id": "price_pro", "unit_amount": 2000},
                    "quantity": 2,
                    # current_period_start / current_period_end deliberately absent
                }
            ]
        },
    }

    with (
        patch("stripe.Subscription.retrieve", return_value=sub),
        patch("stripe.Subscription.modify"),
        patch("stripe.Price.retrieve", return_value=_retrieved_price()),
        patch(
            "stripe.SubscriptionSchedule.create", return_value={"id": "sub_sched_fb"}
        ) as mock_create,
        patch("stripe.SubscriptionSchedule.modify") as mock_sched_modify,
    ):
        result = await change_plan(
            stripe_subscription_id="sub_fallback",
            new_stripe_price_id="price_basic",
            new_price_amount=999,
        )

    assert result == "scheduled_for_period_end"
    mock_create.assert_called_once_with(from_subscription="sub_fallback")
    phases = mock_sched_modify.call_args.kwargs["phases"]
    assert phases[0]["start_date"] == 1_700_000_000
    assert phases[0]["end_date"] == 1_702_592_000
