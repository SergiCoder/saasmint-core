"""Organization role hierarchy and permission checks."""

from __future__ import annotations

from saasmint_core.domain.org import OrgRole
from saasmint_core.exceptions import InsufficientPermissionError

ORG_ROLE_RANK: dict[OrgRole, int] = {
    OrgRole.OWNER: 3,
    OrgRole.ADMIN: 2,
    OrgRole.MEMBER: 1,
}


def check_can_manage_member(*, caller_role: OrgRole, target_role: OrgRole) -> None:
    """Raise if the caller cannot modify/remove the target member."""
    if ORG_ROLE_RANK[target_role] >= ORG_ROLE_RANK[caller_role]:
        raise InsufficientPermissionError("Insufficient permissions for this action.")


def check_can_assign_role(*, caller_role: OrgRole, new_role: OrgRole) -> None:
    """Raise if the caller cannot assign the requested role."""
    if ORG_ROLE_RANK[new_role] >= ORG_ROLE_RANK[caller_role]:
        raise InsufficientPermissionError("Cannot assign a role equal to or above your own.")
