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

    def test_unknown_product_is_ignored(self, user):
        from uuid import uuid4

        from asgiref.sync import async_to_sync

        from apps.billing.models import CreditBalance
        from apps.billing.services import on_product_checkout_completed

        async_to_sync(on_product_checkout_completed)("cs_x", uuid4(), user.id, None)
        assert not CreditBalance.objects.filter(user=user).exists()
