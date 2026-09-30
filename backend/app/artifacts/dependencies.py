"""Artifact dependency graph resolution."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.artifacts.domain import (
    ArtifactCatalog,
    DependencyKind,
    ResolutionStatus,
)

_READY_STATUSES = {
    ResolutionStatus.BOUND.value,
    ResolutionStatus.DEGRADED.value,
    "complete",
    "succeeded",
}
_TERMINAL_STATUSES = {
    *[status.value for status in ResolutionStatus],
    "complete",
    "succeeded",
}


def _status_from_token(token: str) -> tuple[str, str] | None:
    for separator in (":", "="):
        if separator not in token:
            continue
        key, status = token.rsplit(separator, 1)
        if status in _TERMINAL_STATUSES:
            return key, status
    return None


def _is_resolved(
    dependency_key: str,
    resolved: set[str] | Mapping[str, Any],
) -> bool:
    if isinstance(resolved, Mapping):
        status = resolved.get(dependency_key)
        return status is not None and str(status) in _READY_STATUSES

    if dependency_key in resolved:
        return True
    for token in resolved:
        parsed = _status_from_token(str(token))
        if parsed is not None and parsed[0] == dependency_key:
            return parsed[1] in _READY_STATUSES
    return False


@dataclass(frozen=True, slots=True)
class ArtifactDependencyGraph:
    catalog: ArtifactCatalog

    def ready_keys(
        self,
        task_keys: set[str],
        resolved: set[str] | Mapping[str, Any],
    ) -> tuple[str, ...]:
        ready: list[str] = []
        for definition in self.catalog.definitions:
            if definition.artifact_key not in task_keys:
                continue
            if _is_resolved(definition.artifact_key, resolved):
                continue
            for dependency in definition.depends_on:
                if dependency.kind == DependencyKind.ARTIFACT:
                    self.catalog.get(
                        dependency.key,
                        dependency.output_profile or "",
                    )
                if not _is_resolved(dependency.key, resolved):
                    break
            else:
                ready.append(definition.artifact_key)
        return tuple(ready)
