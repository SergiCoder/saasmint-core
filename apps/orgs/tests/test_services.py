"""Tests for apps.orgs.services — org lifecycle, slug generation, invitations."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.orgs.services import (
    _cancel_team_subscription,
    _create_org_with_owner,
    decrement_subscription_seats,
    delete_org,
    delete_org_on_subscription_cancel,
    delete_orgs_created_by_user,
    generate_unique_slug,
    on_team_checkout_completed,
)
from apps.users.models import User

# ---------------------------------------------------------------------------
# generate_unique_slug
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGenerateUniqueSlug:
    def test_simple_name(self):
        slug = generate_unique_slug("My Team")
        assert slug == "my-team"

    def test_strips_special_characters(self):
        slug = generate_unique_slug("Hello @World!")
        assert slug == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        slug = generate_unique_slug("---test---")
        assert slug == "test"

    def test_short_name_falls_back_to_org(self):
        slug = generate_unique_slug("A")
        assert slug == "org"

    def test_empty_name_falls_back_to_org(self):
        slug = generate_unique_slug("!@#")
        assert slug == "org"

    def test_appends_suffix_on_collision(self):
        user = User.objects.create_user(
            email="slug-test@example.com",
            full_name="Slug Test",
        )
        Org.objects.create(name="Taken", slug="taken", created_by=user)
        slug = generate_unique_slug("Taken")
        assert slug == "taken-2"

    def test_reuses_slug_after_hard_delete(self):
        user = User.objects.create_user(
            email="slug-del@example.com",
            full_name="Slug Del",
        )
        org = Org.objects.create(name="Deleted", slug="deleted", created_by=user)
        org.delete()
        slug = generate_unique_slug("Deleted")
        assert slug == "deleted"

    def test_increments_suffix_on_multiple_collisions(self):
        user = User.objects.create_user(
            email="multi@example.com",
            full_name="Multi",
        )
        Org.objects.create(name="Org", slug="org", created_by=user)
        Org.objects.create(name="Org 2", slug="org-2", created_by=user)
        slug = generate_unique_slug("Org")
        assert slug == "org-3"


# ---------------------------------------------------------------------------
# _create_org_with_owner
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateOrgWithOwner:
    def test_creates_org_and_owner_membership(self):
        user = User.objects.create_user(
            email="owner@example.com",
            full_name="Owner",
        )
        org, member = _create_org_with_owner(user, "New Org")
        assert org.name == "New Org"
        assert org.created_by == user
        assert member.role == OrgRole.OWNER
        assert member.is_billing is True

    def test_creates_owner_membership(self) -> None:
        """A user without prior org membership becomes an owner of the new
        org; the OrgMember row is the authoritative signal that this user
        is now an org member."""
        user = User.objects.create_user(
            email="upgrade@example.com",
            full_name="Upgrade",
        )

        org, member = _create_org_with_owner(user, "Upgrade Org")

        assert OrgMember.objects.filter(user=user, org=org, role=OrgRole.OWNER).exists()
        assert org.created_by == user
        assert member.role == OrgRole.OWNER

    def test_creates_org_scoped_stripe_customer(self) -> None:
        """Team checkout passes a fresh Stripe customer ID; the row is
        created org-scoped (no user linkage). Personal subs on a separate
        user-scoped customer are unaffected."""
        from apps.billing.models import StripeCustomer

        user = User.objects.create_user(
            email="freshcust@example.com",
            full_name="Fresh",
        )

        org, _ = _create_org_with_owner(
            user, "Fresh Org", stripe_customer_id="cus_fresh", livemode=True
        )

        customer = StripeCustomer.objects.get(stripe_id="cus_fresh")
        assert customer.user_id is None
        assert customer.org_id == org.id
        assert customer.livemode is True

    def test_second_owner_membership_for_same_user_raises(self) -> None:
        """Rule 8 (``uniq_org_owner_per_user``) is enforced at the DB layer —
        even if the view-layer guard is bypassed by a TOCTOU race, a second
        OWNER row for the same user across two orgs cannot land. The test
        bypasses ``_create_org_with_owner`` and inserts directly to keep the
        assertion at the constraint level."""
        from django.db.utils import IntegrityError

        user = User.objects.create_user(
            email="dual-owner@example.com",
            full_name="Dual Owner",
        )
        org1 = Org.objects.create(name="Org1", slug="dual-owner-1", created_by=user)
        OrgMember.objects.create(org=org1, user=user, role=OrgRole.OWNER)

        org2 = Org.objects.create(name="Org2", slug="dual-owner-2", created_by=user)
        with pytest.raises(IntegrityError, match="uniq_org_owner_per_user"):
            OrgMember.objects.create(org=org2, user=user, role=OrgRole.OWNER)

    def test_duplicate_webhook_is_idempotent(self) -> None:
        """A second checkout.session.completed delivery must not raise — it
        should return the org+membership already created on the first call."""
        user = User.objects.create_user(
            email="dup@example.com",
            full_name="Dup",
        )

        org1, member1 = _create_org_with_owner(
            user, "Dup Org", stripe_customer_id="cus_dup", livemode=False
        )
        org2, member2 = _create_org_with_owner(
            user, "Dup Org", stripe_customer_id="cus_dup", livemode=False
        )

        assert org1.id == org2.id
        assert member1.id == member2.id
        assert Org.objects.filter(name="Dup Org").count() == 1


# ---------------------------------------------------------------------------
# on_team_checkout_completed — personal-sub cancel-at-period-end (PR 5)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnTeamCheckoutCompleted:
    """The webhook callback wires two things together: org creation
    (covered in TestCreateOrgWithOwner) and the optional auto-cancel of
    the user's existing personal subscription.

    The PR 5 default ``keep_personal_subscription=False`` cancels personal
    at period end. ``True`` leaves it running (rule 5b)."""

    @patch("apps.orgs.services._persist_team_subscription", new_callable=AsyncMock)
    @patch("saasmint_core.services.billing.cancel_subscription", new_callable=AsyncMock)
    def test_default_schedules_personal_cancel_at_period_end(
        self, mock_cancel: AsyncMock, _mock_persist: AsyncMock
    ) -> None:
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="upgrader@example.com",
            full_name="Upgrader",
        )
        personal_customer = StripeCustomer.objects.create(
            stripe_id="cus_personal", user=user, livemode=False
        )
        personal_plan = Plan.objects.create(
            name="Personal", context="personal", interval="month", is_active=True
        )
        PlanPrice.objects.create(plan=personal_plan, stripe_price_id="price_personal", amount=999)
        Subscription.objects.create(
            stripe_id="sub_personal",
            stripe_customer=personal_customer,
            user=user,
            status="active",
            plan=personal_plan,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        async_to_sync(on_team_checkout_completed)(
            user.id,
            "Upgrade Org",
            "cus_team_fresh",
            False,
            "sub_team_fresh",
            False,  # keep_personal_subscription
        )

        mock_cancel.assert_awaited_once()
        kwargs = mock_cancel.await_args.kwargs
        assert kwargs["stripe_customer_id"] == personal_customer.id
        assert kwargs["at_period_end"] is True

    @patch("apps.orgs.services._persist_team_subscription", new_callable=AsyncMock)
    @patch("saasmint_core.services.billing.cancel_subscription", new_callable=AsyncMock)
    def test_keep_flag_skips_personal_cancel(
        self, mock_cancel: AsyncMock, _mock_persist: AsyncMock
    ) -> None:
        """Opt-out path: user explicitly chose concurrent billing. The
        callback must not touch the personal sub even if one exists."""
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="keeper@example.com",
            full_name="Keeper",
        )
        personal_customer = StripeCustomer.objects.create(
            stripe_id="cus_keep_personal", user=user, livemode=False
        )
        personal_plan = Plan.objects.create(
            name="Personal Keep", context="personal", interval="month", is_active=True
        )
        PlanPrice.objects.create(plan=personal_plan, stripe_price_id="price_keep", amount=999)
        Subscription.objects.create(
            stripe_id="sub_keep_personal",
            stripe_customer=personal_customer,
            user=user,
            status="active",
            plan=personal_plan,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        async_to_sync(on_team_checkout_completed)(
            user.id,
            "Keep Org",
            "cus_team_keep_fresh",
            False,
            "sub_team_keep_fresh",
            True,  # keep_personal_subscription
        )

        mock_cancel.assert_not_awaited()

    @patch("apps.orgs.services._persist_team_subscription", new_callable=AsyncMock)
    @patch("saasmint_core.services.billing.cancel_subscription", new_callable=AsyncMock)
    def test_no_personal_customer_is_noop(
        self, mock_cancel: AsyncMock, _mock_persist: AsyncMock
    ) -> None:
        """User with no personal Stripe customer (e.g. straight-to-team
        signup): the default flag still runs through the helper, but it
        no-ops without raising."""
        user = User.objects.create_user(
            email="legacy@example.com",
            full_name="Legacy",
        )

        async_to_sync(on_team_checkout_completed)(
            user.id,
            "Legacy Org",
            "cus_team_legacy",
            False,
            "sub_team_legacy",
            False,
        )

        mock_cancel.assert_not_awaited()

    def test_no_active_personal_sub_swallows_not_found(self) -> None:
        """User has a personal Stripe customer but no active sub on it
        (e.g. their previous personal sub was already canceled). The
        helper catches ``SubscriptionNotFoundError`` and continues."""
        from apps.billing.models import StripeCustomer

        user = User.objects.create_user(
            email="orphan@example.com",
            full_name="Orphan",
        )
        StripeCustomer.objects.create(stripe_id="cus_orphan", user=user, livemode=False)

        # No mock — the real cancel_subscription should raise
        # SubscriptionNotFoundError internally and the helper should swallow it.
        async_to_sync(on_team_checkout_completed)(
            user.id,
            "Orphan Org",
            "cus_team_orphan",
            False,
            None,
            False,
        )

        # If we got here without raising, the no-op path works.
        assert Org.objects.filter(name="Orphan Org").exists()


@pytest.mark.django_db
class TestPersistTeamSubscription:
    """Stripe sometimes delivers ``customer.subscription.created`` BEFORE
    ``checkout.session.completed`` — the sync webhook fails because the
    StripeCustomer row hasn't been written yet. ``on_team_checkout_completed``
    closes that race by retrieving the subscription and upserting the row
    directly after org creation."""

    def _make_sub_dict(self, stripe_sub_id: str, stripe_customer_id: str, price_id: str) -> dict:
        """Plain-dict shape — matches what the webhook dispatcher receives
        (events are JSON-decoded by ``stripe.Webhook.construct_event``)."""
        period_start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
        period_end = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        return {
            "id": stripe_sub_id,
            "customer": stripe_customer_id,
            "status": "active",
            "items": {
                "data": [
                    {
                        "price": {"id": price_id},
                        "quantity": 2,
                        "current_period_start": period_start,
                        "current_period_end": period_end,
                    }
                ]
            },
            "trial_end": None,
            "canceled_at": None,
            "cancel_at": None,
        }

    def _make_stripe_subscription(self, stripe_sub_id: str, stripe_customer_id: str, price_id: str):
        """Real StripeObject — matches what ``stripe.Subscription.retrieve``
        returns. Exercises the boundary conversion in ``_persist_team_subscription``,
        which would otherwise crash with ``AttributeError: get`` because
        StripeObject proxies ``.get(...)`` through ``__getattr__``."""
        import stripe

        return stripe.StripeObject.construct_from(
            self._make_sub_dict(stripe_sub_id, stripe_customer_id, price_id),
            "sk_test_unused",
        )

    @patch("apps.orgs.services.stripe.Subscription.retrieve")
    def test_persists_team_subscription_after_org_creation(self, mock_retrieve) -> None:
        """The webhook handler must land the team Subscription row even if
        the matching ``customer.subscription.created`` event raced and was
        marked failed. After ``on_team_checkout_completed`` runs, the local
        Subscription mirror exists, scoped to the new org's StripeCustomer."""
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="raced@example.com",
            full_name="Raced",
        )
        team_plan = Plan.objects.create(
            name="Team Basic", context="team", interval="month", is_active=True
        )
        PlanPrice.objects.create(plan=team_plan, stripe_price_id="price_team_raced", amount=2500)

        mock_retrieve.return_value = self._make_stripe_subscription(
            "sub_team_raced", "cus_team_raced", "price_team_raced"
        )

        async_to_sync(on_team_checkout_completed)(
            user.id,
            "Raced Org",
            "cus_team_raced",
            False,
            "sub_team_raced",
            True,  # keep_personal_subscription — skip the cancel branch
        )

        customer = StripeCustomer.objects.get(stripe_id="cus_team_raced")
        assert customer.org_id is not None
        sub = Subscription.objects.get(stripe_id="sub_team_raced")
        assert sub.stripe_customer_id == customer.id
        assert sub.user_id is None  # team sub — no user mirror
        assert sub.plan_id == team_plan.id
        assert sub.seat_limit == 2
        mock_retrieve.assert_called_once_with("sub_team_raced")

    @patch("apps.orgs.services.stripe.Subscription.retrieve")
    def test_persist_is_idempotent_with_later_webhook(self, mock_retrieve) -> None:
        """If ``customer.subscription.created`` later succeeds (or
        ``customer.subscription.updated`` arrives), the upsert finds the
        existing row by stripe_id and updates it — no duplicate."""
        from saasmint_core.services.webhooks import sync_subscription_from_data

        from apps.billing.models import Plan, PlanPrice, Subscription
        from apps.billing.repositories import get_webhook_repos

        user = User.objects.create_user(
            email="idem-sub@example.com",
            full_name="Idem Sub",
        )
        team_plan = Plan.objects.create(
            name="Team Idem", context="team", interval="month", is_active=True
        )
        PlanPrice.objects.create(plan=team_plan, stripe_price_id="price_team_idem", amount=2500)
        mock_retrieve.return_value = self._make_stripe_subscription(
            "sub_team_idem", "cus_team_idem", "price_team_idem"
        )

        # First write via the team-checkout handler (StripeObject path).
        async_to_sync(on_team_checkout_completed)(
            user.id, "Idem Org", "cus_team_idem", False, "sub_team_idem", True
        )

        # Second write via the (delayed) ``customer.subscription.updated``
        # webhook path with a different quantity. Same stripe_id → row
        # updates, not duplicates. Webhook events arrive as plain dicts.
        update_payload = self._make_sub_dict("sub_team_idem", "cus_team_idem", "price_team_idem")
        update_payload["items"]["data"][0]["quantity"] = 5
        repos = get_webhook_repos()
        async_to_sync(sync_subscription_from_data)(
            update_payload,
            customers=repos.customers,
            plans=repos.plans,
            subscriptions=repos.subscriptions,
        )

        rows = Subscription.objects.filter(stripe_id="sub_team_idem")
        assert rows.count() == 1
        assert rows.first().seat_limit == 5


# ---------------------------------------------------------------------------
# delete_org_on_subscription_cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrgOnSubscriptionCancel:
    """The webhook callback is dispatch-only — the cascade body lives in the
    Celery task and is tested in ``apps.orgs.tests.test_tasks``."""

    @patch("apps.orgs.tasks.delete_org_on_subscription_cancel_task.delay")
    def test_dispatches_task_with_stringified_org_id(self, mock_delay):
        org_id = uuid4()
        async_to_sync(delete_org_on_subscription_cancel)(org_id)
        mock_delay.assert_called_once_with(str(org_id))


# ---------------------------------------------------------------------------
# delete_org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrg:
    @patch("apps.orgs.services._cancel_team_subscription")
    def test_hard_deletes_org_and_members(self, mock_cancel):
        user = User.objects.create_user(
            email="delorg@example.com",
            full_name="Del Org",
        )
        org = Org.objects.create(name="DelOrg", slug="delorg", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER, is_billing=True)
        member = User.objects.create_user(
            email="delmember@example.com",
            full_name="Del Member",
        )
        OrgMember.objects.create(org=org, user=member, role=OrgRole.MEMBER)
        org_id = org.id
        user_id = user.id
        member_id = member.id

        delete_org(org)

        assert not Org.objects.filter(id=org_id).exists()
        assert not User.objects.filter(id=user_id).exists()
        assert not User.objects.filter(id=member_id).exists()
        assert not OrgMember.objects.filter(org_id=org_id).exists()


# ---------------------------------------------------------------------------
# delete_orgs_created_by_user
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteOrgsCreatedByUser:
    @patch("apps.orgs.services._cancel_team_subscription")
    def test_deletes_all_active_orgs(self, mock_cancel):
        """``delete_orgs_created_by_user`` filters by ``Org.created_by`` and
        is independent of OrgMember role — a user who's only ever owned one
        org at a time (rule 8) can still have *created* multiple orgs over
        their lifetime via ownership transfers. This setup mirrors that:
        same ``created_by`` on both orgs, but only the OWNER constraint-
        respecting first one carries an OrgMember row."""
        user = User.objects.create_user(
            email="multiorg@example.com",
            full_name="Multi Org",
        )
        org1 = Org.objects.create(name="Org1", slug="org1", created_by=user)
        OrgMember.objects.create(org=org1, user=user, role=OrgRole.OWNER)
        org2 = Org.objects.create(name="Org2", slug="org2", created_by=user)
        org1_id = org1.id
        org2_id = org2.id

        delete_orgs_created_by_user(user.id)

        assert not Org.objects.filter(id=org1_id).exists()
        assert not Org.objects.filter(id=org2_id).exists()


# ---------------------------------------------------------------------------
# decrement_subscription_seats
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDecrementSubscriptionSeats:
    def test_no_stripe_customer_is_noop(self):
        """No error when org has no Stripe customer."""
        decrement_subscription_seats(uuid4())

    @patch("apps.orgs.services.async_to_sync")
    def test_calls_update_seat_count(self, mock_async_to_sync):
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="seats@example.com",
            full_name="Seats",
        )
        org = Org.objects.create(name="Seats Org", slug="seats-org", created_by=user)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.OWNER)
        customer = StripeCustomer.objects.create(stripe_id="cus_seats", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_seats", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_seats",
            stripe_customer=customer,
            status="active",
            plan=plan,
            seat_limit=3,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        mock_update = MagicMock()
        mock_async_to_sync.return_value = mock_update

        decrement_subscription_seats(org.id)

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.kwargs["quantity"] == 1  # 1 member (owner)


# ---------------------------------------------------------------------------
# _cancel_team_subscription
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelTeamSubscription:
    def test_no_customer_is_noop(self):
        user = User.objects.create_user(
            email="nocust@example.com",
            full_name="No Cust",
        )
        org = Org.objects.create(name="NoCust", slug="nocust", created_by=user)
        _cancel_team_subscription(org)  # should not raise

    @patch("stripe.Subscription.cancel")
    def test_cancels_stripe_subscription(self, mock_cancel):
        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        user = User.objects.create_user(
            email="cancelsub@example.com",
            full_name="Cancel Sub",
        )
        org = Org.objects.create(name="CancelSub", slug="cancelsub", created_by=user)
        customer = StripeCustomer.objects.create(stripe_id="cus_cancel", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_cancel", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_cancel",
            stripe_customer=customer,
            status="active",
            plan=plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        _cancel_team_subscription(org)
        mock_cancel.assert_called_once_with("sub_cancel", prorate=False)

    @patch("stripe.Subscription.cancel", side_effect=Exception("Stripe error"))
    def test_logs_error_on_stripe_failure(self, mock_cancel):
        import stripe

        from apps.billing.models import Plan, PlanPrice, StripeCustomer, Subscription

        mock_cancel.side_effect = stripe.StripeError("fail")

        user = User.objects.create_user(
            email="failcancel@example.com",
            full_name="Fail Cancel",
        )
        org = Org.objects.create(name="FailCancel", slug="failcancel", created_by=user)
        customer = StripeCustomer.objects.create(stripe_id="cus_fail", org=org, livemode=False)
        plan = Plan.objects.create(name="Team", context="team", interval="month", is_active=True)
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_fail", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_fail",
            stripe_customer=customer,
            status="active",
            plan=plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        # Should not raise — logs the error
        _cancel_team_subscription(org)
        mock_cancel.assert_called_once()
