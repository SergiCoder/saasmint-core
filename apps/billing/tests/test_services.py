"""Tests for apps.billing.services — credits grant + product checkout."""

from __future__ import annotations

import pytest

from apps.users.models import User

# ---------------------------------------------------------------------------
# Credits: grant_credits_for_session + on_product_checkout_completed
# ---------------------------------------------------------------------------
# `user` fixture is provided by apps/billing/tests/conftest.py.


@pytest.fixture
def org_member(db):
    return User.objects.create_user(
        email="owner@example.com",
        full_name="Owner",
    )


@pytest.fixture
def org(org_member):
    from apps.orgs.models import Org, OrgMember, OrgRole

    org = Org.objects.create(name="Credit Org", slug="credit-org", created_by=org_member)
    OrgMember.objects.create(org=org, user=org_member, role=OrgRole.OWNER, is_billing=True)
    return org


@pytest.fixture
def boost_product(db):
    from apps.billing.models import Product, ProductType

    return Product.objects.create(
        name="100 Credits", type=ProductType.ONE_TIME, credits=100, is_active=True
    )


@pytest.mark.django_db
class TestGrantCreditsForSession:
    def test_first_call_grants_credits(self, user):
        from apps.billing.models import CreditBalance, CreditTransaction
        from apps.billing.services import grant_credits_for_session

        granted = grant_credits_for_session(
            stripe_session_id="cs_one", amount=50, reason="purchase:Test", user=user
        )
        assert granted is True
        assert CreditBalance.objects.get(user=user).balance == 50
        assert CreditTransaction.objects.filter(stripe_session_id="cs_one").count() == 1

    def test_duplicate_session_id_is_noop(self, user):
        """Same stripe_session_id must not double-credit — gives us free
        idempotency for duplicate webhook deliveries."""
        from apps.billing.models import CreditBalance, CreditTransaction
        from apps.billing.services import grant_credits_for_session

        assert (
            grant_credits_for_session(
                stripe_session_id="cs_dup", amount=50, reason="purchase:Test", user=user
            )
            is True
        )
        assert (
            grant_credits_for_session(
                stripe_session_id="cs_dup", amount=50, reason="purchase:Test", user=user
            )
            is False
        )

        assert CreditBalance.objects.get(user=user).balance == 50
        assert CreditTransaction.objects.filter(stripe_session_id="cs_dup").count() == 1

    def test_org_scope_routes_to_org_balance(self, org):
        from apps.billing.models import CreditBalance
        from apps.billing.services import grant_credits_for_session

        granted = grant_credits_for_session(
            stripe_session_id="cs_org", amount=200, reason="purchase:Team", org=org
        )
        assert granted is True
        assert CreditBalance.objects.get(org=org).balance == 200

    def test_rejects_both_user_and_org(self, user, org):
        from apps.billing.services import grant_credits_for_session

        with pytest.raises(ValueError, match="Exactly one"):
            grant_credits_for_session(
                stripe_session_id="cs_bad",
                amount=1,
                reason="x",
                user=user,
                org=org,
            )

    def test_rejects_non_positive_amount(self, user):
        from apps.billing.services import grant_credits_for_session

        with pytest.raises(ValueError, match="positive amount"):
            grant_credits_for_session(stripe_session_id="cs_zero", amount=0, reason="x", user=user)


@pytest.mark.django_db
class TestOnProductCheckoutCompleted:
    def test_personal_purchase_credits_the_user(self, user, boost_product):
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)("cs_personal", boost_product.id, user.id, None)
        assert CreditBalance.objects.get(user=user).balance == boost_product.credits

    def test_team_purchase_credits_the_org(self, org_member, org, boost_product):
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)(
            "cs_team", boost_product.id, org_member.id, org.id
        )
        assert CreditBalance.objects.get(org=org).balance == boost_product.credits
        assert not CreditBalance.objects.filter(user=org_member).exists()

    def test_duplicate_session_id_is_noop_for_org_scope(
        self, org_member, org, boost_product
    ):
        """Same idempotency contract as the user-scoped duplicate test, but
        for ``org_id`` purchases: a replayed webhook with the same
        ``stripe_session_id`` must not double-credit the org. The unique
        constraint on ``CreditTransaction.stripe_session_id`` makes the
        second call a no-op regardless of scope."""
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance, CreditTransaction
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)(
            "cs_team_dup", boost_product.id, org_member.id, org.id
        )
        async_to_sync(on_product_checkout_completed)(
            "cs_team_dup", boost_product.id, org_member.id, org.id
        )

        # Org balance reflects exactly one grant — duplicate was suppressed.
        assert CreditBalance.objects.get(org=org).balance == boost_product.credits
        # And exactly one CreditTransaction row exists for that session id.
        assert (
            CreditTransaction.objects.filter(stripe_session_id="cs_team_dup").count()
            == 1
        )
        # No personal balance was minted as a side-effect.
        assert not CreditBalance.objects.filter(user=org_member).exists()

    def test_unknown_product_is_ignored(self, user):
        from uuid import uuid4

        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)("cs_x", uuid4(), user.id, None)
        assert not CreditBalance.objects.filter(user=user).exists()

    def test_zero_credits_product_is_skipped(self, user):
        """A product whose ``credits`` value is 0 (misconfigured or a
        non-credit one-time product) must not grant any credits and must
        not raise — the webhook path treats it as a no-op."""
        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance, Product, ProductType
        from apps.billing.services import on_product_checkout_completed

        zero_product = Product.objects.create(
            name="Zero Credits",
            type=ProductType.ONE_TIME,
            credits=0,
            is_active=True,
        )
        async_to_sync(on_product_checkout_completed)("cs_zero", zero_product.id, user.id, None)
        assert not CreditBalance.objects.filter(user=user).exists()

    def test_unknown_org_id_is_skipped(self, user, boost_product):
        """When the webhook carries an ``org_id`` that no longer exists in
        the DB (org deleted between checkout and webhook delivery), the
        handler silently no-ops — no credits granted, no exception raised."""
        from uuid import uuid4

        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        phantom_org_id = uuid4()
        async_to_sync(on_product_checkout_completed)(
            "cs_noorg", boost_product.id, user.id, phantom_org_id
        )
        assert not CreditBalance.objects.filter(user=user).exists()

    def test_unknown_user_id_is_skipped(self, boost_product):
        """When the webhook's ``user_id`` no longer exists (account deleted
        between checkout and webhook delivery), the handler no-ops."""
        from uuid import uuid4

        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        phantom_user_id = uuid4()
        async_to_sync(on_product_checkout_completed)(
            "cs_nouser", boost_product.id, phantom_user_id, None
        )
        # No balance row should be created anywhere.
        assert CreditBalance.objects.count() == 0


@pytest.mark.django_db
class TestGetCreditBalance:
    """Tests for the ``get_credit_balance`` helper covering branches not
    exercised by the view-layer tests: the ``org=`` Org-object path and the
    guard that rejects ambiguous / empty call signatures."""

    def test_returns_balance_for_user(self, user):
        from apps.billing.models import CreditBalance
        from apps.billing.services import get_credit_balance

        CreditBalance.objects.create(user=user, balance=42)
        assert get_credit_balance(user=user) == 42

    def test_returns_zero_when_no_user_row(self, user):
        from apps.billing.services import get_credit_balance

        assert get_credit_balance(user=user) == 0

    def test_returns_balance_via_org_object(self):
        """Callers that hold a full ``Org`` instance can pass ``org=`` instead
        of ``org_id=``. This branch (line 40 in services.py) is never hit by
        the view which always uses ``org_id=``."""
        from apps.billing.models import CreditBalance
        from apps.billing.services import get_credit_balance
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="gb-owner@example.com", full_name="GB Owner")
        org = Org.objects.create(name="GB Org", slug="gb-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)
        CreditBalance.objects.create(org=org, balance=99)

        assert get_credit_balance(org=org) == 99

    def test_raises_when_no_args_given(self):
        from apps.billing.services import get_credit_balance

        with pytest.raises(ValueError, match="Exactly one"):
            get_credit_balance()

    def test_raises_when_multiple_args_given(self, user):
        from apps.billing.services import get_credit_balance
        from apps.orgs.models import Org, OrgMember, OrgRole
        from apps.users.models import User

        owner = User.objects.create_user(email="multi-owner@example.com", full_name="Multi")
        org = Org.objects.create(name="Multi Org", slug="multi-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)

        with pytest.raises(ValueError, match="Exactly one"):
            get_credit_balance(user=user, org=org)
