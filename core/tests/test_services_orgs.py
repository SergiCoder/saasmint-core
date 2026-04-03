"""Tests for services/orgs.py — role rank checks."""

from __future__ import annotations

import pytest

from saasmint_core.domain.org import OrgRole
from saasmint_core.exceptions import InsufficientPermissionError
from saasmint_core.services.orgs import (
    ORG_ROLE_RANK,
    check_can_assign_role,
    check_can_manage_member,
)

# ── ORG_ROLE_RANK constant ──────────────────────────────────────────────────


def test_role_rank_has_all_roles() -> None:
    assert set(ORG_ROLE_RANK) == {OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER}


def test_role_rank_ordering() -> None:
    assert ORG_ROLE_RANK[OrgRole.OWNER] > ORG_ROLE_RANK[OrgRole.ADMIN]
    assert ORG_ROLE_RANK[OrgRole.ADMIN] > ORG_ROLE_RANK[OrgRole.MEMBER]


# ── check_can_manage_member ──────────────────────────────────────────────────


def test_owner_can_manage_admin() -> None:
    check_can_manage_member(caller_role=OrgRole.OWNER, target_role=OrgRole.ADMIN)


def test_owner_can_manage_member() -> None:
    check_can_manage_member(caller_role=OrgRole.OWNER, target_role=OrgRole.MEMBER)


def test_admin_can_manage_member() -> None:
    check_can_manage_member(caller_role=OrgRole.ADMIN, target_role=OrgRole.MEMBER)


def test_admin_cannot_manage_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.ADMIN, target_role=OrgRole.OWNER)


def test_admin_cannot_manage_admin() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.ADMIN, target_role=OrgRole.ADMIN)


def test_member_cannot_manage_member() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.MEMBER, target_role=OrgRole.MEMBER)


def test_member_cannot_manage_admin() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.MEMBER, target_role=OrgRole.ADMIN)


def test_member_cannot_manage_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.MEMBER, target_role=OrgRole.OWNER)


def test_owner_cannot_manage_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_manage_member(caller_role=OrgRole.OWNER, target_role=OrgRole.OWNER)


# ── check_can_assign_role ────────────────────────────────────────────────────


def test_owner_can_assign_admin() -> None:
    check_can_assign_role(caller_role=OrgRole.OWNER, new_role=OrgRole.ADMIN)


def test_owner_can_assign_member() -> None:
    check_can_assign_role(caller_role=OrgRole.OWNER, new_role=OrgRole.MEMBER)


def test_admin_can_assign_member() -> None:
    check_can_assign_role(caller_role=OrgRole.ADMIN, new_role=OrgRole.MEMBER)


def test_owner_cannot_assign_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.OWNER, new_role=OrgRole.OWNER)


def test_admin_cannot_assign_admin() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.ADMIN, new_role=OrgRole.ADMIN)


def test_admin_cannot_assign_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.ADMIN, new_role=OrgRole.OWNER)


def test_member_cannot_assign_member() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.MEMBER, new_role=OrgRole.MEMBER)


def test_member_cannot_assign_admin() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.MEMBER, new_role=OrgRole.ADMIN)


def test_member_cannot_assign_owner() -> None:
    with pytest.raises(InsufficientPermissionError):
        check_can_assign_role(caller_role=OrgRole.MEMBER, new_role=OrgRole.OWNER)
