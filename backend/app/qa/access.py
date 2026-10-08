from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.auth.service import AuthUser

_READ_ROLES = frozenset(
    {
        "superadmin",
        "group_leader",
        "group_deputy",
        "group_member",
        "viewer",
    }
)
_RESTRICTED_MODEL_DENY = frozenset({"restricted"})


class AccessPolicy:
    """Central authorization decisions for QA retrieval and tool execution."""

    def can_read_event(self, user: AuthUser, event_id: Any | None = None) -> bool:
        del event_id
        return bool(user.is_active and user.role in _READ_ROLES)

    def can_read_source(self, user: AuthUser, source: Any) -> bool:
        if not self.can_read_event(user):
            return False
        if source is None or not _value(source, "is_active", True):
            return False
        return _value(source, "access_level", "internal") in {
            "public",
            "internal",
            "restricted",
        }

    def can_export_to_model(self, source: Any) -> bool:
        if source is None or not _value(source, "is_active", True):
            return False
        return _value(source, "access_level", "internal") not in _RESTRICTED_MODEL_DENY


def _value(source: Any, name: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


__all__ = ["AccessPolicy"]
