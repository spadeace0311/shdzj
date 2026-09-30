"""Versioned professional artifact catalog loading and validation."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from app.artifacts.domain import (
    ArtifactCatalog,
    ArtifactDefinition,
    ArtifactKind,
    DependencyKind,
    DependencySpec,
)

EXPECTED_ARTIFACT_KEYS = (
    "map.shelter_emergency",
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.pga_zoning",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.reservoirs",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.metro",
    "map.seismic_stations",
    "map.active_faults",
    "map.building_damage",
    "map.building_grid",
    "map.rescue_teams",
    "map.cultural_relics",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
    "doc.background",
    "doc.housing",
    "doc.economy",
    "doc.population",
    "doc.key_targets",
    "doc.spatial_distances",
    "doc.area_overview",
    "doc.historical_catalog",
    "doc.rapid_brief",
    "doc.rapid_report",
    "doc.decision_report",
    "deck.decision_report",
)

A_CLASS_MAPS = (
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.active_faults",
    "map.building_damage",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
)

B_CLASS_MAPS = (
    "map.shelter_emergency",
    "map.pga_zoning",
    "map.reservoirs",
    "map.metro",
    "map.seismic_stations",
    "map.rescue_teams",
    "map.cultural_relics",
)

PROFILE = "a3v-professional"
MAP_DIMENSIONS = (4761, 3369)
MAP_DPI = 300
RAPID_REPORT_PRODUCT_KEYS = (
    "intensity.fusion",
    "loss.buildings",
    "loss.population",
    "loss.casualties",
    "loss.economic",
    "loss.resources",
    "loss.validate",
)
BUILDING_GRID_REQUIRED_ASSETS = (
    "shanghai.admin.town",
    "shanghai.building.town",
    "basemap.gaode.offline",
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


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label} must contain non-empty strings")
        result.append(item)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(result)


def _dependency_tuple(value: Any, label: str) -> tuple[DependencySpec, ...]:
    items = _sequence(value, label)
    dependencies: list[DependencySpec] = []
    for index, item in enumerate(items):
        try:
            dependency = DependencySpec.from_dict(_mapping(item, f"{label}[{index}]"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {label}[{index}]: {error}") from error
        dependencies.append(dependency)
    identities = [
        (dependency.kind, dependency.key, dependency.output_profile)
        for dependency in dependencies
    ]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} cannot contain duplicates")
    return tuple(dependencies)


def _dimensions(value: Any, label: str) -> tuple[int, int] | None:
    if value is None:
        return None
    items = _sequence(value, label)
    if len(items) != 2 or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in items
    ):
        raise ValueError(f"{label} must contain two positive integers")
    return items[0], items[1]


def _parse_definition(
    artifact_key: str,
    raw_definition: Mapping[str, Any],
) -> ArtifactDefinition:
    try:
        kind = ArtifactKind(raw_definition["kind"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"{artifact_key} has an invalid kind") from error

    output_profile = raw_definition.get("output_profile")
    if not isinstance(output_profile, str) or not output_profile:
        raise ValueError(f"{artifact_key} output_profile must be a non-empty string")

    priority = raw_definition.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority <= 0:
        raise ValueError(f"{artifact_key} priority must be a positive integer")

    dpi = raw_definition.get("dpi")
    if dpi is not None and (isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0):
        raise ValueError(f"{artifact_key} dpi must be a positive integer")

    page_size = raw_definition.get("page_size")
    if page_size is not None and not isinstance(page_size, str):
        raise ValueError(f"{artifact_key} page_size must be a string or null")

    string_fields: dict[str, str] = {}
    for field_name in (
        "display_name",
        "format",
        "legacy_code",
        "template_key",
        "marker_policy",
        "failure_policy",
        "quality_policy",
    ):
        value = raw_definition.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{artifact_key} {field_name} must be a non-empty string")
        string_fields[field_name] = value
    if string_fields["failure_policy"] not in {"block", "degrade"}:
        raise ValueError(f"{artifact_key} failure_policy must be block or degrade")
    if string_fields["marker_policy"] != "mode":
        raise ValueError(f"{artifact_key} marker_policy must be mode")

    return ArtifactDefinition(
        artifact_key=artifact_key,
        display_name=string_fields["display_name"],
        kind=kind,
        output_profile=output_profile,
        format=string_fields["format"].lower(),
        legacy_code=string_fields["legacy_code"],
        priority=priority,
        depends_on=_dependency_tuple(
            raw_definition.get("depends_on", []),
            f"{artifact_key}.depends_on",
        ),
        optional_depends_on=_dependency_tuple(
            raw_definition.get("optional_depends_on", []),
            f"{artifact_key}.optional_depends_on",
        ),
        required_assets=_string_tuple(
            raw_definition.get("required_assets", []),
            f"{artifact_key}.required_assets",
        ),
        optional_assets=_string_tuple(
            raw_definition.get("optional_assets", []),
            f"{artifact_key}.optional_assets",
        ),
        template_key=string_fields["template_key"],
        marker_policy=string_fields["marker_policy"],
        failure_policy=string_fields["failure_policy"],
        quality_policy=string_fields["quality_policy"],
        dimensions=_dimensions(
            raw_definition.get("dimensions"),
            f"{artifact_key}.dimensions",
        ),
        dpi=dpi,
        page_size=page_size,
        degrade_conditions=_string_tuple(
            raw_definition.get("degrade_conditions", []),
            f"{artifact_key}.degrade_conditions",
        ),
    )


def _validate_catalog(catalog: ArtifactCatalog) -> None:
    ordered_keys = tuple(
        definition.artifact_key for definition in catalog.definitions
    )
    if ordered_keys != EXPECTED_ARTIFACT_KEYS:
        raise ValueError("artifact catalog must contain exactly the 39 required keys in order")

    expected_codes = tuple(
        [f"M{index:02d}" for index in range(1, 28)]
        + [f"D{index:02d}" for index in range(1, 13)]
    )
    actual_codes = tuple(
        definition.legacy_code for definition in catalog.definitions
    )
    if actual_codes != expected_codes:
        raise ValueError("artifact legacy codes must be M01-M27 followed by D01-D12")

    for definition in catalog.definitions:
        if definition.output_profile != PROFILE:
            raise ValueError(
                f"unsupported output profile {definition.output_profile!r} "
                f"for {definition.artifact_key}"
            )
        if definition.kind == ArtifactKind.MAP:
            if definition.format not in {"jpg", "png"}:
                raise ValueError(f"{definition.artifact_key} map format must be jpg or png")
            if definition.dimensions != MAP_DIMENSIONS or definition.dpi != MAP_DPI:
                raise ValueError(
                    f"{definition.artifact_key} must use {MAP_DIMENSIONS} at {MAP_DPI} DPI"
                )
        elif definition.kind == ArtifactKind.DOCX:
            if definition.format != "docx":
                raise ValueError(f"{definition.artifact_key} must use docx")
        elif definition.kind == ArtifactKind.PPTX and definition.format != "pptx":
            raise ValueError(f"{definition.artifact_key} must use pptx")

        hard_identities = {
            (
                dependency.kind,
                dependency.key,
                dependency.output_profile,
            )
            for dependency in definition.depends_on
        }
        optional_identities = {
            (
                dependency.kind,
                dependency.key,
                dependency.output_profile,
            )
            for dependency in definition.optional_depends_on
        }
        if hard_identities & optional_identities:
            raise ValueError(
                f"{definition.artifact_key} has a dependency in both hard and optional lists"
            )
        if set(definition.required_assets) & set(definition.optional_assets):
            raise ValueError(
                f"{definition.artifact_key} has an asset in both required and optional lists"
            )
        if (
            "spatial_allocation_rule" in definition.required_assets
            or "spatial_allocation_rule" in definition.optional_assets
        ):
            raise ValueError(
                f"{definition.artifact_key} cannot register a model allocation rule "
                "as a data asset"
            )
        for dependency in (*definition.depends_on, *definition.optional_depends_on):
            if dependency.kind == DependencyKind.ARTIFACT:
                catalog.get(dependency.key, dependency.output_profile or "")

    for key in A_CLASS_MAPS:
        definition = catalog.get(key, PROFILE)
        if definition.quality_policy != "A" or definition.failure_policy != "block":
            raise ValueError(f"A-class map {key} must use quality A and block failures")
        if key == "map.transport":
            if (
                definition.optional_assets != ("shanghai.road.network",)
                or definition.degrade_conditions != ("optional_asset_missing",)
            ):
                raise ValueError(
                    "map.transport requires only road network optional degradation"
                )
        elif definition.optional_assets or definition.degrade_conditions:
            raise ValueError(
                f"A-class map {key} cannot declare the M11 degradation exception"
            )

    for key in B_CLASS_MAPS:
        definition = catalog.get(key, PROFILE)
        if definition.quality_policy != "B" or definition.failure_policy != "degrade":
            raise ValueError(f"B-class map {key} must use quality B and degrade")

    building_grid = catalog.get("map.building_grid", PROFILE)
    if (
        building_grid.quality_policy != "C"
        or building_grid.failure_policy != "degrade"
        or building_grid.required_assets != BUILDING_GRID_REQUIRED_ASSETS
        or building_grid.degrade_conditions != ("spatialized_estimate",)
    ):
        raise ValueError("map.building_grid requires conditional spatialized degradation")

    rapid_report = catalog.get("doc.rapid_report", PROFILE)
    hard_artifact_keys = {
        dependency.key
        for dependency in rapid_report.depends_on
        if dependency.kind == DependencyKind.ARTIFACT
    }
    hard_product_keys = {
        dependency.key
        for dependency in rapid_report.depends_on
        if dependency.kind == DependencyKind.ASSESSMENT_PRODUCT
    }
    optional_artifact_keys = {
        dependency.key
        for dependency in rapid_report.optional_depends_on
        if dependency.kind == DependencyKind.ARTIFACT
    }
    expected_background = {
        "doc.background",
        "doc.housing",
        "doc.economy",
        "doc.population",
        "doc.key_targets",
        "doc.spatial_distances",
        "doc.area_overview",
        "doc.historical_catalog",
    }
    if hard_artifact_keys != expected_background | set(A_CLASS_MAPS):
        raise ValueError("doc.rapid_report has an invalid A-class dependency set")
    if hard_product_keys != set(RAPID_REPORT_PRODUCT_KEYS):
        raise ValueError(
            "doc.rapid_report has an invalid assessment product dependency set"
        )
    if optional_artifact_keys != set(B_CLASS_MAPS) | {"map.building_grid"}:
        raise ValueError("doc.rapid_report has an invalid optional dependency set")

    catalog.assert_acyclic()


def load_catalog(path: str | Path) -> ArtifactCatalog:
    catalog_path = Path(path)
    if not catalog_path.is_file():
        raise FileNotFoundError(f"artifact catalog does not exist: {catalog_path}")
    raw = _load_yaml(catalog_path) or {}
    root = _mapping(raw, "artifact catalog")
    catalog_version = root.get("catalog_version")
    if not isinstance(catalog_version, str) or not catalog_version:
        raise ValueError("catalog_version must be a non-empty string")

    raw_definitions = _mapping(root.get("artifacts"), "artifacts")
    for index, artifact_key in enumerate(raw_definitions):
        if not isinstance(artifact_key, str) or not artifact_key:
            raise ValueError(
                f"artifacts key at index {index} must be a non-empty string: "
                f"{artifact_key!r}"
            )
    if tuple(raw_definitions) != EXPECTED_ARTIFACT_KEYS:
        raise ValueError(
            "artifacts must contain exactly the 39 required keys in order"
        )
    definitions = tuple(
        _parse_definition(artifact_key, _mapping(definition, artifact_key))
        for artifact_key, definition in raw_definitions.items()
    )
    catalog = ArtifactCatalog(
        catalog_version=catalog_version,
        definitions=definitions,
    )
    _validate_catalog(catalog)
    return catalog
