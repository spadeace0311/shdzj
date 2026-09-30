"""Database-independent domain types for artifact production."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ArtifactKind(StrEnum):
    MAP = "map"
    DOCX = "docx"
    PPTX = "pptx"


class ProductionMode(StrEnum):
    LIVE = "live"
    MANUAL = "manual"
    TEST = "test"
    DRILL = "drill"
    REPLAY = "replay"


class LaunchMode(StrEnum):
    ASSESSMENT_CHILD = "assessment_child"
    STANDALONE = "standalone"


class ProductionRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"


class ProductionTaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"


class PublicationMode(StrEnum):
    AUTOMATIC = "automatic"
    REBUILD = "rebuild"
    SUPERADMIN_OVERRIDE = "superadmin_override"


class DependencyKind(StrEnum):
    ASSESSMENT_PRODUCT = "assessment_product"
    ARTIFACT = "artifact"


class ResolutionStatus(StrEnum):
    BOUND = "bound"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    OMITTED_AFTER_WAIT = "omitted_after_wait"


@dataclass(frozen=True, slots=True, eq=False)
class DependencySpec:
    """A typed dependency persisted in task dependency arrays."""

    kind: DependencyKind
    key: str
    output_profile: str | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("dependency key cannot be empty")
        if self.kind == DependencyKind.ASSESSMENT_PRODUCT and self.output_profile is not None:
            raise ValueError("assessment product dependencies cannot have an output profile")
        if self.kind == DependencyKind.ARTIFACT and not self.output_profile:
            raise ValueError("artifact dependencies require an output profile")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "key": self.key,
            "output_profile": self.output_profile,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DependencySpec":
        raw_kind = value.get("kind")
        kind = raw_kind if isinstance(raw_kind, DependencyKind) else DependencyKind(raw_kind)
        key = value.get("key")
        if key is None:
            key = value.get("product_key", value.get("artifact_key"))
        if not isinstance(key, str):
            raise ValueError("dependency key must be a string")
        output_profile = value.get("output_profile")
        if output_profile is not None and not isinstance(output_profile, str):
            raise ValueError("dependency output profile must be a string or null")
        return cls(kind=kind, key=key, output_profile=output_profile)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DependencySpec):
            return (
                self.kind,
                self.key,
                self.output_profile,
            ) == (
                other.kind,
                other.key,
                other.output_profile,
            )
        if isinstance(other, Mapping):
            return self.to_dict() == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.kind, self.key, self.output_profile))


@dataclass(frozen=True, slots=True)
class ArtifactDefinition:
    artifact_key: str
    display_name: str
    kind: ArtifactKind
    output_profile: str
    format: str
    legacy_code: str
    priority: int
    depends_on: tuple[DependencySpec, ...]
    optional_depends_on: tuple[DependencySpec, ...]
    required_assets: tuple[str, ...]
    optional_assets: tuple[str, ...]
    template_key: str
    marker_policy: str
    failure_policy: str
    quality_policy: str
    dimensions: tuple[int, int] | None = None
    dpi: int | None = None
    page_size: str | None = None
    degrade_conditions: tuple[str, ...] = ()
    template_package: str | None = None
    control_fields: tuple[str, ...] = ()

    @property
    def is_conditional_degrade(self) -> bool:
        return bool(self.degrade_conditions)

    def allows_degraded_output(
        self,
        *,
        condition: str | None = None,
        spatialized_estimate: bool | None = None,
    ) -> bool:
        if condition is not None:
            if condition not in self.degrade_conditions:
                return False
            if condition == "spatialized_estimate":
                return spatialized_estimate is True
            return True
        if self.failure_policy != "degrade":
            return False
        if "spatialized_estimate" in self.degrade_conditions:
            return spatialized_estimate is True
        return True


@dataclass(frozen=True, slots=True)
class ArtifactNameContext:
    place: str
    magnitude: float
    display_name: str
    version: int
    generated_at: str | datetime
    production_mode: ProductionMode


@dataclass(frozen=True, slots=True)
class ArtifactCatalog:
    catalog_version: str
    definitions: tuple[ArtifactDefinition, ...]
    _by_key: Mapping[tuple[str, str], ArtifactDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        by_key: dict[tuple[str, str], ArtifactDefinition] = {}
        for definition in self.definitions:
            identity = (definition.artifact_key, definition.output_profile)
            if identity in by_key:
                raise ValueError(
                    f"duplicate artifact definition: {definition.artifact_key}:"
                    f"{definition.output_profile}"
                )
            by_key[identity] = definition
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))

    @property
    def version(self) -> str:
        return self.catalog_version

    @classmethod
    def load(cls, path: str | Any) -> "ArtifactCatalog":
        from app.artifacts.catalog import load_catalog

        return load_catalog(path)

    def get(self, artifact_key: str, output_profile: str) -> ArtifactDefinition:
        try:
            return self._by_key[(artifact_key, output_profile)]
        except KeyError as error:
            raise KeyError(
                f"unknown artifact output: {artifact_key}:{output_profile}"
            ) from error

    def full_required_outputs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (definition.artifact_key, definition.output_profile)
            for definition in self.definitions
        )

    def scope_required_outputs(
        self,
        scope: str,
        default_profile: str,
    ) -> tuple[tuple[str, str], ...]:
        if scope == "full":
            return self.full_required_outputs()

        parts = scope.split(":")
        if len(parts) != 3 or parts[0] != "artifact" or not parts[1] or not parts[2]:
            raise ValueError(
                "artifact scope must be 'artifact:<artifact_key>:<output_profile>'"
            )
        artifact_key, output_profile = parts[1:]
        try:
            self.get(artifact_key, output_profile)
        except KeyError:
            raise ValueError(
                f"unknown artifact output: {artifact_key}:{output_profile}"
            ) from None
        return ((artifact_key, output_profile),)

    def assert_acyclic(self) -> None:
        visiting: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()

        def visit(identity: tuple[str, str]) -> None:
            if identity in visited:
                return
            if identity in visiting:
                raise ValueError(f"artifact dependency cycle at {identity[0]}")
            visiting.add(identity)
            definition = self._by_key[identity]
            for dependency in (
                *definition.depends_on,
                *definition.optional_depends_on,
            ):
                if dependency.kind != DependencyKind.ARTIFACT:
                    continue
                dependency_identity = (dependency.key, dependency.output_profile or "")
                if dependency_identity not in self._by_key:
                    raise ValueError(
                        f"artifact dependency is not in catalog: {dependency.key}:"
                        f"{dependency.output_profile}"
                    )
                visit(dependency_identity)
            visiting.remove(identity)
            visited.add(identity)

        for identity in self._by_key:
            visit(identity)
