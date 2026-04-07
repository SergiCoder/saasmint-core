"""Tests for the seed_dev_data management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
class TestSeedDevDataGuard:
    def test_blocked_when_debug_false(self, settings):
        settings.DEBUG = False
        err = StringIO()
        call_command("seed_dev_data", stderr=err)
        assert "seed_dev_data can only run with DEBUG=True" in err.getvalue()

    def test_allowed_when_debug_true(self, settings):
        settings.DEBUG = True
        out = StringIO()
        # Should complete without raising
        call_command("seed_dev_data", stdout=out)
        assert "seeded successfully" in out.getvalue()


@pytest.mark.django_db
class TestSeedDevDataCreatesExpectedObjects:
    def setup_method(self):
        from django.conf import settings

        settings.DEBUG = True

    def test_creates_plans(self, settings):
        from apps.billing.models import Plan

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        names = set(Plan.objects.values_list("name", flat=True))
        assert "Personal Free" in names
        assert "Personal Pro" in names
        assert "Team Basic" in names
        assert "Team Pro" in names

    def test_creates_plan_prices(self, settings):
        from apps.billing.models import PlanPrice

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert PlanPrice.objects.count() == 5  # one per plan

    def test_creates_personal_users(self, settings):
        from apps.users.models import User

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert User.objects.filter(email="jack.bauer@ctu.gov").exists()
        assert User.objects.filter(email="ethan.hunt@imf.gov").exists()
        # 10 personal users defined in PERSONAL_USERS fixture
        assert User.objects.count() >= 10

    def test_creates_stripe_customers_for_users(self, settings):
        from apps.billing.models import StripeCustomer

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert StripeCustomer.objects.filter(stripe_id="cus_dev_jack_bauer").exists()

    def test_creates_subscriptions_with_correct_statuses(self, settings):
        from apps.billing.models import Subscription, SubscriptionStatus

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        # Jack Bauer → active personal subscription
        sub = Subscription.objects.get(stripe_id="sub_dev_jack_bauer")
        assert sub.status == SubscriptionStatus.ACTIVE

        # Deckard Shaw → trialing
        sub_trial = Subscription.objects.get(stripe_id="sub_dev_deckard_shaw")
        assert sub_trial.status == SubscriptionStatus.TRIALING
        assert sub_trial.trial_ends_at is not None

        # Bryan Mills → canceled
        sub_canceled = Subscription.objects.get(stripe_id="sub_dev_bryan_mills")
        assert sub_canceled.status == SubscriptionStatus.CANCELED
        assert sub_canceled.canceled_at is not None

    def test_creates_orgs(self, settings):
        from apps.orgs.models import Org

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert Org.objects.filter(slug="ctu").exists()
        assert Org.objects.filter(slug="imf").exists()
        assert Org.objects.filter(slug="hobbs-shaw").exists()
        assert Org.objects.filter(slug="continental").exists()

    def test_creates_org_memberships(self, settings):
        from apps.orgs.models import OrgMember, OrgRole

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        # Jack Bauer is owner of CTU
        from apps.orgs.models import Org
        from apps.users.models import User

        ctu = Org.objects.get(slug="ctu")
        jack = User.objects.get(email="jack.bauer@ctu.gov")
        membership = OrgMember.objects.get(org=ctu, user=jack)
        assert membership.role == OrgRole.OWNER

    def test_creates_org_stripe_customers(self, settings):
        from apps.billing.models import StripeCustomer

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert StripeCustomer.objects.filter(stripe_id="cus_dev_org_ctu").exists()

    def test_org_subscription_with_seats(self, settings):
        from apps.billing.models import Subscription

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        sub = Subscription.objects.get(stripe_id="sub_dev_org_ctu")
        assert sub.quantity == 5


@pytest.mark.django_db
class TestSeedDevDataIdempotency:
    def test_running_twice_does_not_duplicate_plans(self, settings):
        from apps.billing.models import Plan

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        count_after_first = Plan.objects.count()

        call_command("seed_dev_data", stdout=StringIO())
        count_after_second = Plan.objects.count()

        assert count_after_first == count_after_second

    def test_running_twice_does_not_duplicate_users(self, settings):
        from apps.users.models import User

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        count_after_first = User.objects.count()

        call_command("seed_dev_data", stdout=StringIO())
        count_after_second = User.objects.count()

        assert count_after_first == count_after_second

    def test_running_twice_does_not_duplicate_orgs(self, settings):
        from apps.orgs.models import Org

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        count_after_first = Org.objects.count()

        call_command("seed_dev_data", stdout=StringIO())
        count_after_second = Org.objects.count()

        assert count_after_first == count_after_second

    def test_running_twice_does_not_duplicate_subscriptions(self, settings):
        from apps.billing.models import Subscription

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        count_after_first = Subscription.objects.count()

        call_command("seed_dev_data", stdout=StringIO())
        count_after_second = Subscription.objects.count()

        assert count_after_first == count_after_second
