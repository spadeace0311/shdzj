"""Versioned task-template catalog loading and applicability evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import ArtifactCatalog
from app.collaboration.domain import WorkgroupCode
from app.events.domain import EventKind

PHASE_CODES: Final[frozenset[str]] = frozenset(
    {
        "within_30m",
        "30m_to_60m",
        "1h_to_2h",
        "2h_to_4h",
        "4h_to_end",
        "1h_to_end",
        "2h_to_end",
    }
)
DEFAULT_ARTIFACT_CATALOG_PATH: Final = "/config/artifacts/catalog.yaml"
# The task brief's sample uses this lookup name; the emitted stable code remains
# technology.professional_outputs from the authoritative YAML list.
_INTENSITY_MAP_ALIAS: Final = "technology.professional_outputs"
_SPATIAL_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "inside_shanghai",
        "boundary_20km",
        "outside_shanghai",
        "outside_assessment_scope",
        "assessment_scope",
        "any",
    }
)
_APPLICABILITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "event_kind",
        "event_kinds",
        "event_types",
        "institutional_level",
        "institutional_levels",
        "service_level",
        "service_levels",
        "spatial_class",
        "minimum_magnitude",
        "maximum_magnitude",
        "minimum_depth_km",
        "maximum_depth_km",
        "minimum_max_intensity",
        "intensity_threshold",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    artifact_key: str
    output_profile: str

    def __post_init__(self) -> None:
        if not self.artifact_key.strip():
            raise ValueError("artifact binding key must not be empty")
        if not self.output_profile.strip():
            raise ValueError("artifact binding output profile must not be empty")


@dataclass(frozen=True, slots=True)
class TaskTemplateDefinition:
    template_code: str
    workgroup_code: WorkgroupCode
    phase_code: str
    title: str
    source: str
    start_offset_seconds: int = 0
    due_offset_seconds: int | None = None
    continues_until_response_end: bool = False
    required_deliverables: tuple[str, ...] = ()
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    applicability: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    priority: int = 100
    instruction: str = ""
    response_basis: str | None = None


@dataclass(frozen=True, slots=True)
class TaskTemplateCatalog:
    version: str
    definitions: tuple[TaskTemplateDefinition, ...]
    _by_code: Mapping[str, TaskTemplateDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("task template version must not be empty")
        by_code: dict[str, TaskTemplateDefinition] = {}
        for definition in self.definitions:
            if definition.template_code in by_code:
                raise ValueError(
                    f"duplicate task code: {definition.template_code}"
                )
            by_code[definition.template_code] = definition
        object.__setattr__(self, "_by_code", MappingProxyType(by_code))

    def get(self, template_code: str) -> TaskTemplateDefinition:
        resolved_code = (
            _INTENSITY_MAP_ALIAS
            if template_code == "technology.intensity_map"
            else template_code
        )
        try:
            return self._by_code[resolved_code]
        except KeyError as error:
            raise KeyError(f"unknown task template: {template_code}") from error

    def to_snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "definitions": [
                {
                    "template_code": definition.template_code,
                    "workgroup_code": definition.workgroup_code.value,
                    "phase_code": definition.phase_code,
                    "title": definition.title,
                    "source": definition.source,
                    "start_offset_seconds": definition.start_offset_seconds,
                    "due_offset_seconds": definition.due_offset_seconds,
                    "continues_until_response_end": (
                        definition.continues_until_response_end
                    ),
                    "required_deliverables": list(
                        definition.required_deliverables
                    ),
                    "artifact_bindings": [
                        {
                            "artifact_key": binding.artifact_key,
                            "output_profile": binding.output_profile,
                        }
                        for binding in definition.artifact_bindings
                    ],
                    "applicability": _snapshot_json_compatible(
                        dict(definition.applicability)
                    ),
                    "priority": definition.priority,
                    "instruction": definition.instruction or None,
                    "response_basis": definition.response_basis,
                }
                for definition in self.definitions
            ],
        }

    @classmethod
    def from_snapshot(cls, snapshot: object) -> TaskTemplateCatalog:
        raw = _mapping(snapshot, "template catalog snapshot")
        version = _required_text(
            raw.get("version"),
            "template catalog snapshot.version",
        )
        raw_definitions = _sequence(
            raw.get("definitions"),
            "template catalog snapshot.definitions",
        )
        return cls(
            version=version,
            definitions=tuple(
                _definition_from_snapshot(
                    _mapping(
                        raw_definition,
                        f"template catalog snapshot.definitions[{index}]",
                    ),
                    index,
                )
                for index, raw_definition in enumerate(raw_definitions)
            ),
        )

    def get_applicable(
        self,
        event: object,
        revision: object,
        *,
        intensity_threshold: Decimal | str | None = None,
    ) -> tuple[TaskTemplateDefinition, ...]:
        event_kind = _event_kind(event, revision)
        if event_kind is EventKind.AUTO:
            return ()
        return tuple(
            definition
            for definition in self.definitions
            if _definition_applicable(
                definition,
                event,
                revision,
                event_kind=event_kind,
                intensity_threshold=intensity_threshold,
            )
        )


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _StrictSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from error
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _resolve_config_file(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    repo_root = Path(__file__).resolve().parents[3]
    fallback = repo_root / candidate.as_posix().lstrip("/")
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(f"task template catalog does not exist: {candidate}")


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictSafeLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(
            error,
            "context_mark",
            None,
        )
        if mark is not None:
            detail = getattr(error, "problem", None) or str(error)
            raise ValueError(
                f"invalid YAML in {path} at line {mark.line + 1}, "
                f"column {mark.column + 1}: {detail}"
            ) from error
        raise ValueError(f"invalid YAML in {path}: {error}") from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must not be negative")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_required_text(item, f"{label}[{index}]"))
    if len(result) != len(set(result)):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(result)


def _artifact_binding_tuple(
    value: Any,
    label: str,
) -> tuple[ArtifactBinding, ...]:
    items = _sequence(value, label)
    bindings: list[ArtifactBinding] = []
    for index, item in enumerate(items):
        pair = _sequence(item, f"{label}[{index}]")
        if len(pair) != 2:
            raise ValueError(
                f"{label}[{index}] must contain artifact key and output profile"
            )
        bindings.append(
            ArtifactBinding(
                artifact_key=_required_text(
                    pair[0],
                    f"{label}[{index}].artifact_key",
                ),
                output_profile=_required_text(
                    pair[1],
                    f"{label}[{index}].output_profile",
                ),
            )
        )
    if len(bindings) != len(set(bindings)):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(bindings)


def _applicability(value: Any, label: str) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    raw = _mapping(value, label)
    unknown = set(raw) - _APPLICABILITY_KEYS
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {sorted(unknown)}")
    normalized: dict[str, object] = {}
    for key, raw_value in raw.items():
        if key == "spatial_class":
            spatial_class = _required_text(raw_value, f"{label}.{key}")
            if spatial_class not in _SPATIAL_CLASSES:
                raise ValueError(
                    f"{label}.{key} has unknown value: {spatial_class}"
                )
            normalized[key] = spatial_class
        elif key in {"event_kind", "institutional_level", "service_level"}:
            normalized[key] = _required_text(raw_value, f"{label}.{key}")
        elif key in {
            "event_kinds",
            "event_types",
            "institutional_levels",
            "service_levels",
        }:
            normalized[key] = _string_tuple(raw_value, f"{label}.{key}")
        else:
            normalized[key] = _decimal(raw_value, f"{label}.{key}")
    return MappingProxyType(normalized)


def _snapshot_json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _snapshot_json_compatible(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_snapshot_json_compatible(item) for item in value]
    return value


def _definition_from_snapshot(
    raw_definition: Mapping[str, Any],
    index: int,
) -> TaskTemplateDefinition:
    label = f"template catalog snapshot.definitions[{index}]"
    template_code = _required_text(
        raw_definition.get("template_code"),
        f"{label}.template_code",
    )
    raw_group = _required_text(
        raw_definition.get("workgroup_code"),
        f"{label}.workgroup_code",
    )
    try:
        workgroup_code = WorkgroupCode(raw_group)
    except ValueError as error:
        raise ValueError(
            f"{label} has unknown workgroup: {raw_group}"
        ) from error
    phase_code = _required_text(
        raw_definition.get("phase_code"),
        f"{label}.phase_code",
    )
    if phase_code not in PHASE_CODES:
        raise ValueError(f"{label} has unknown phase: {phase_code}")
    start_offset_seconds = _integer(
        raw_definition.get("start_offset_seconds", 0),
        f"{label}.start_offset_seconds",
        minimum=0,
    )
    raw_due = raw_definition.get("due_offset_seconds")
    due_offset_seconds = (
        None
        if raw_due is None
        else _integer(
            raw_due,
            f"{label}.due_offset_seconds",
            minimum=0,
        )
    )
    if (
        due_offset_seconds is not None
        and due_offset_seconds < start_offset_seconds
    ):
        raise ValueError(
            f"{label}.due_offset_seconds must be greater than or equal to "
            "start_offset_seconds"
        )
    artifact_bindings: list[ArtifactBinding] = []
    raw_bindings = _sequence(
        raw_definition.get("artifact_bindings", []),
        f"{label}.artifact_bindings",
    )
    for binding_index, raw_binding in enumerate(raw_bindings):
        binding = _mapping(
            raw_binding,
            f"{label}.artifact_bindings[{binding_index}]",
        )
        artifact_bindings.append(
            ArtifactBinding(
                artifact_key=_required_text(
                    binding.get("artifact_key"),
                    (
                        f"{label}.artifact_bindings"
                        f"[{binding_index}].artifact_key"
                    ),
                ),
                output_profile=_required_text(
                    binding.get("output_profile"),
                    (
                        f"{label}.artifact_bindings"
                        f"[{binding_index}].output_profile"
                    ),
                ),
            )
        )
    if len(artifact_bindings) != len(set(artifact_bindings)):
        raise ValueError(f"{label}.artifact_bindings cannot contain duplicates")

    return TaskTemplateDefinition(
        template_code=template_code,
        workgroup_code=workgroup_code,
        phase_code=phase_code,
        title=_required_text(raw_definition.get("title"), f"{label}.title"),
        source=_required_text(raw_definition.get("source"), f"{label}.source"),
        start_offset_seconds=start_offset_seconds,
        due_offset_seconds=due_offset_seconds,
        continues_until_response_end=_boolean(
            raw_definition.get("continues_until_response_end", False),
            f"{label}.continues_until_response_end",
        ),
        required_deliverables=_string_tuple(
            raw_definition.get("required_deliverables", []),
            f"{label}.required_deliverables",
        ),
        artifact_bindings=tuple(artifact_bindings),
        applicability=_applicability(
            raw_definition.get("applicability"),
            f"{label}.applicability",
        ),
        priority=_integer(
            raw_definition.get("priority", 100),
            f"{label}.priority",
        ),
        instruction=(
            _optional_text(
                raw_definition.get("instruction"),
                f"{label}.instruction",
            )
            or ""
        ),
        response_basis=_optional_text(
            raw_definition.get("response_basis"),
            f"{label}.response_basis",
        ),
    )


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _parse_definition(
    raw_definition: Mapping[str, Any],
    index: int,
    artifact_catalog: ArtifactCatalog,
) -> TaskTemplateDefinition:
    label = f"tasks[{index}]"
    try:
        template_code = _required_text(raw_definition["code"], f"{label}.code")
        raw_group = _required_text(raw_definition["group"], f"{label}.group")
        try:
            workgroup_code = WorkgroupCode(raw_group)
        except ValueError as error:
            raise ValueError(
                f"{label} has unknown workgroup: {raw_group}"
            ) from error
        phase_code = _required_text(raw_definition["phase"], f"{label}.phase")
        if phase_code not in PHASE_CODES:
            raise ValueError(f"{label} has unknown phase: {phase_code}")
        title = _required_text(raw_definition["title"], f"{label}.title")
        source = _required_text(raw_definition["source"], f"{label}.source")
        start_offset_seconds = _integer(
            raw_definition.get("start_offset_seconds", 0),
            f"{label}.start_offset_seconds",
            minimum=0,
        )
        raw_due = raw_definition.get("due_offset_seconds")
        due_offset_seconds = (
            None
            if raw_due is None
            else _integer(
                raw_due,
                f"{label}.due_offset_seconds",
                minimum=0,
            )
        )
        if (
            due_offset_seconds is not None
            and due_offset_seconds < start_offset_seconds
        ):
            raise ValueError(
                f"{label}.due_offset_seconds must be greater than or equal to "
                "start_offset_seconds"
            )
        required_deliverables = _string_tuple(
            raw_definition.get("required_deliverables", []),
            f"{label}.required_deliverables",
        )
        artifact_bindings = _artifact_binding_tuple(
            raw_definition.get("artifacts", []),
            f"{label}.artifacts",
        )
        for binding in artifact_bindings:
            try:
                artifact_catalog.get(
                    binding.artifact_key,
                    binding.output_profile,
                )
            except KeyError as error:
                raise ValueError(
                    f"{label} has unknown artifact: "
                    f"{binding.artifact_key}:{binding.output_profile}"
                ) from error
        applicability = _applicability(
            raw_definition.get("applicability"),
            f"{label}.applicability",
        )
    except KeyError as error:
        raise ValueError(f"{label} is missing field {error.args[0]}") from error

    return TaskTemplateDefinition(
        template_code=template_code,
        workgroup_code=workgroup_code,
        phase_code=phase_code,
        title=title,
        source=source,
        start_offset_seconds=start_offset_seconds,
        due_offset_seconds=due_offset_seconds,
        continues_until_response_end=_boolean(
            raw_definition.get("continuous", False),
            f"{label}.continuous",
        ),
        required_deliverables=required_deliverables,
        artifact_bindings=artifact_bindings,
        applicability=applicability,
        priority=_integer(raw_definition.get("priority", 100), f"{label}.priority"),
        instruction=_optional_text(raw_definition.get("instruction"), f"{label}.instruction")
        or "",
        response_basis=_optional_text(
            raw_definition.get("response_basis"),
            f"{label}.response_basis",
        ),
    )


def load_task_template_catalog(path: str | Path) -> TaskTemplateCatalog:
    catalog_path = _resolve_config_file(path)
    raw = _load_yaml(catalog_path) or {}
    root = _mapping(raw, "task template catalog")
    version = _required_text(root.get("version"), "version")
    raw_definitions = _sequence(root.get("tasks"), "tasks")
    artifact_catalog = load_catalog(DEFAULT_ARTIFACT_CATALOG_PATH)
    definitions = tuple(
        _parse_definition(
            _mapping(raw_definition, f"tasks[{index}]"),
            index,
            artifact_catalog,
        )
        for index, raw_definition in enumerate(raw_definitions)
    )
    return TaskTemplateCatalog(version=version, definitions=definitions)


def _event_kind(event: object, revision: object) -> EventKind | None:
    value = getattr(revision, "revision_kind", None)
    if value is None:
        value = getattr(event, "event_type", None)
    if isinstance(value, EventKind):
        return value
    try:
        return EventKind(value)
    except (TypeError, ValueError):
        return None


def _first_attr(revision: object, event: object, name: str) -> object:
    value = getattr(revision, name, None)
    if value is not None:
        return value
    return getattr(event, name, None)


def _definition_applicable(
    definition: TaskTemplateDefinition,
    event: object,
    revision: object,
    *,
    event_kind: EventKind | None,
    intensity_threshold: Decimal | str | None = None,
) -> bool:
    applicability = definition.applicability
    if event_kind is not None:
        allowed_event_kinds = _applicability_values(
            applicability,
            "event_kind",
            "event_kinds",
            alternate_key="event_types",
        )
        if allowed_event_kinds and event_kind.value not in allowed_event_kinds:
            return False

    institutional_level = _first_attr(
        revision,
        event,
        "institutional_level",
    )
    allowed_institutional = _applicability_values(
        applicability,
        "institutional_level",
        "institutional_levels",
    )
    if (
        allowed_institutional
        and str(institutional_level) not in allowed_institutional
    ):
        return False

    service_level = _first_attr(revision, event, "service_level")
    allowed_service = _applicability_values(
        applicability,
        "service_level",
        "service_levels",
    )
    if allowed_service and str(service_level) not in allowed_service:
        return False

    inside_shanghai = _first_attr(revision, event, "inside_shanghai")
    distance = _coerce_optional_decimal(
        _first_attr(revision, event, "distance_to_boundary_km")
    )
    spatial_class = applicability.get("spatial_class")

    magnitude = _coerce_optional_decimal(
        _first_attr(revision, event, "magnitude")
    )
    if not _within_numeric_bounds(
        magnitude,
        minimum=applicability.get("minimum_magnitude"),
        maximum=applicability.get("maximum_magnitude"),
    ):
        return False

    depth = _coerce_optional_decimal(_first_attr(revision, event, "depth_km"))
    if not _within_numeric_bounds(
        depth,
        minimum=applicability.get("minimum_depth_km"),
        maximum=applicability.get("maximum_depth_km"),
    ):
        return False

    normal_scope = _normal_task_scope(
        applicability,
        event,
        revision,
        inside_shanghai=inside_shanghai,
        distance=distance,
        intensity_threshold=intensity_threshold,
    )
    if spatial_class is None:
        return normal_scope
    if str(spatial_class) == "outside_assessment_scope":
        return not normal_scope
    if not _matches_spatial_class(
        str(spatial_class),
        inside_shanghai=inside_shanghai,
        distance=distance,
    ):
        return False
    return _intensity_applicable(
        applicability,
        event,
        revision,
        intensity_threshold=intensity_threshold,
    )


def _normal_task_scope(
    applicability: Mapping[str, object],
    event: object,
    revision: object,
    *,
    inside_shanghai: object,
    distance: Decimal | None,
    intensity_threshold: Decimal | str | None,
) -> bool:
    return _is_assessment_scope(
        inside_shanghai=inside_shanghai,
        distance=distance,
    ) and _intensity_applicable(
        applicability,
        event,
        revision,
        intensity_threshold=intensity_threshold,
    )


def _intensity_applicable(
    applicability: Mapping[str, object],
    event: object,
    revision: object,
    *,
    intensity_threshold: Decimal | str | None,
) -> bool:

    effective_threshold = intensity_threshold
    if effective_threshold is None:
        effective_threshold = applicability.get("minimum_max_intensity")
    if effective_threshold is None:
        effective_threshold = applicability.get("intensity_threshold")
    if effective_threshold is None:
        return True

    max_intensity = _response_max_intensity(revision, event)
    if max_intensity is None:
        return True
    return max_intensity >= _decimal(
        effective_threshold,
        "intensity threshold",
    )


def _applicability_values(
    applicability: Mapping[str, object],
    singular_key: str,
    plural_key: str,
    *,
    alternate_key: str | None = None,
) -> set[str]:
    value = applicability.get(plural_key)
    if value is None and alternate_key is not None:
        value = applicability.get(alternate_key)
    if value is None:
        value = applicability.get(singular_key)
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}  # type: ignore[union-attr]


def _matches_spatial_class(
    spatial_class: str,
    *,
    inside_shanghai: object,
    distance: Decimal | None,
) -> bool:
    if spatial_class == "any":
        return True
    if spatial_class == "inside_shanghai":
        return inside_shanghai is True
    if spatial_class == "boundary_20km":
        return (
            inside_shanghai is False
            and distance is not None
            and distance <= Decimal("20")
        )
    if spatial_class == "outside_shanghai":
        return inside_shanghai is False
    outside = not _is_assessment_scope(
        inside_shanghai=inside_shanghai,
        distance=distance,
    )
    if spatial_class == "outside_assessment_scope":
        return outside
    return not outside


def _is_assessment_scope(
    *,
    inside_shanghai: object,
    distance: Decimal | None,
) -> bool:
    if inside_shanghai is True:
        return True
    if inside_shanghai is False:
        return distance is not None and distance <= Decimal("20")
    return True


def _coerce_optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return _decimal(value, "applicability value")
    except ValueError:
        return None


def _within_numeric_bounds(
    value: Decimal | None,
    *,
    minimum: object,
    maximum: object,
) -> bool:
    if minimum is not None:
        if value is None or value < _decimal(minimum, "minimum"):
            return False
    if maximum is not None:
        if value is None or value > _decimal(maximum, "maximum"):
            return False
    return True


def _response_max_intensity(revision: object, event: object) -> Decimal | None:
    for source in (revision, event):
        suggestion = getattr(source, "response_suggestion", None)
        if not isinstance(suggestion, Mapping):
            continue
        value = suggestion.get("max_intensity")
        if value is not None:
            return _coerce_optional_decimal(value)
    return None
