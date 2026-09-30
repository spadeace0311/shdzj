from __future__ import annotations

from app.auth.service import AuthUser

EMERGENCY_TECH_WORKGROUP = "应急技术组"
REBUILD_ROLES = {"group_leader", "group_deputy", "group_member"}


def can_rebuild_artifacts(user: AuthUser) -> bool:
    if user.role == "superadmin":
        return True
    return (
        user.workgroup == EMERGENCY_TECH_WORKGROUP
        and user.role in REBUILD_ROLES
    )
