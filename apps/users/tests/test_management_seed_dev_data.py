"""Tests for the seed_dev_data management command.

After the schema cleanup, seed_dev_data delegates catalog seeding to
`seed_catalog` and no longer creates users, orgs, or subscriptions.
"""

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
        call_command("seed_dev_data", stdout=out)
        assert "seeded successfully" in out.getvalue()

    def test_does_not_create_catalog_when_debug_false(self, settings):
        from apps.billing.models import Plan

        settings.DEBUG = False
        call_command("seed_dev_data", stderr=StringIO())
        assert Plan.objects.count() == 0


@pytest.mark.django_db
class TestSeedDevDataCreatesCatalog:
    def test_creates_plans(self, settings):
        from apps.billing.models import Plan

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        names = set(Plan.objects.values_list("name", flat=True))
        assert "Personal Basic" in names
        assert "Personal Pro" in names
        assert "Team Basic" in names
        assert "Team Pro" in names

    def test_creates_plan_prices(self, settings):
        from apps.billing.models import PlanPrice

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        # 4 monthly (basic/pro for personal/team) + 4 yearly variants.
        assert PlanPrice.objects.count() == 8

    def test_does_not_create_users(self, settings):
        from apps.users.models import User

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert User.objects.count() == 0

    def test_does_not_create_orgs(self, settings):
        from apps.orgs.models import Org

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert Org.objects.count() == 0

    def test_does_not_create_subscriptions(self, settings):
        from apps.billing.models import Subscription

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        assert Subscription.objects.count() == 0


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

    def test_running_twice_does_not_duplicate_plan_prices(self, settings):
        from apps.billing.models import PlanPrice

        settings.DEBUG = True
        call_command("seed_dev_data", stdout=StringIO())
        count_after_first = PlanPrice.objects.count()

        call_command("seed_dev_data", stdout=StringIO())
        count_after_second = PlanPrice.objects.count()

        assert count_after_first == count_after_second
