"""Verify Django TextChoices enums stay in sync with core StrEnum definitions.

If a member is added/removed in one place but not the other, these tests fail.
"""

from __future__ import annotations

import pytest
from saasmint_core.domain.org import OrgRole as CoreOrgRole
from saasmint_core.domain.subscription import PlanContext as CorePlanContext
from saasmint_core.domain.subscription import PlanInterval as CorePlanInterval
from saasmint_core.domain.subscription import SubscriptionStatus as CoreSubscriptionStatus
from saasmint_core.domain.user import AccountType as CoreAccountType

from apps.billing.models import PlanContext as DjPlanContext
from apps.billing.models import PlanInterval as DjPlanInterval
from apps.billing.models import SubscriptionStatus as DjSubscriptionStatus
from apps.orgs.models import OrgRole as DjOrgRole
from apps.users.models import AccountType as DjAccountType

_ENUM_PAIRS: list[tuple[type, type, str]] = [
    (DjOrgRole, CoreOrgRole, "OrgRole"),
    (DjSubscriptionStatus, CoreSubscriptionStatus, "SubscriptionStatus"),
    (DjPlanInterval, CorePlanInterval, "PlanInterval"),
    (DjPlanContext, CorePlanContext, "PlanContext"),
    (DjAccountType, CoreAccountType, "AccountType"),
]


@pytest.mark.parametrize(
    ("django_enum", "core_enum", "name"),
    _ENUM_PAIRS,
    ids=[pair[2] for pair in _ENUM_PAIRS],
)
def test_enum_values_match(django_enum: type, core_enum: type, name: str) -> None:
    """Django TextChoices values must exactly match core StrEnum values."""
    django_values = {e.value for e in django_enum}
    core_values = {e.value for e in core_enum}
    assert django_values == core_values, (
        f"{name} drift detected — "
        f"Django-only: {django_values - core_values}, "
        f"Core-only: {core_values - django_values}"
    )


@pytest.mark.parametrize(
    ("django_enum", "core_enum", "name"),
    _ENUM_PAIRS,
    ids=[pair[2] for pair in _ENUM_PAIRS],
)
def test_enum_names_match(django_enum: type, core_enum: type, name: str) -> None:
    """Django TextChoices member names must exactly match core StrEnum member names."""
    django_names = {e.name for e in django_enum}
    core_names = {e.name for e in core_enum}
    assert django_names == core_names, (
        f"{name} member name drift detected — "
        f"Django-only: {django_names - core_names}, "
        f"Core-only: {core_names - django_names}"
    )
