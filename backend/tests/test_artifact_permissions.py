from app.artifacts.permissions import can_rebuild_artifacts
from app.auth.service import AuthUser


def test_superadmin_can_always_rebuild() -> None:
    user = AuthUser(username="admin", role="superadmin", workgroup=None)
    assert can_rebuild_artifacts(user) is True


def test_emergency_tech_members_can_rebuild() -> None:
    user = AuthUser(username="tech", role="group_member", workgroup="应急技术组")
    assert can_rebuild_artifacts(user) is True


def test_other_workgroup_cannot_rebuild() -> None:
    user = AuthUser(username="member", role="group_member", workgroup="综合协调组")
    assert can_rebuild_artifacts(user) is False
