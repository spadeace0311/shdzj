from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.artifacts.repository import ArtifactQuality
from app.config import settings


A_CLASS_ARTIFACTS = (
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

_TRANSPORT_OPTIONAL_ROAD = "shanghai.road.network"
_ACTIVE_FAULT_EMPTY_STATEMENT = "检索范围内无活动断裂记录"


class MapSourceUnavailableError(RuntimeError):
    """Raised when a required map source is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AttributeBinding:
    field: str
    property_name: str | None = None
    value_status: str = "available"
    metric_key: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "field": self.field,
            "property_name": self.property_name,
            "value_status": self.value_status,
            "metric_key": self.metric_key,
        }


@dataclass(frozen=True, slots=True)
class LayerDefinition:
    layer_id: str
    source_key: str
    geometry_type: str
    style_id: str
    legend: tuple[Mapping[str, str], ...] = ()
    minimum_zoom: int = 0
    attribute_bindings: tuple[AttributeBinding, ...] = ()
    optional: bool = False
    verified_empty_statement: str | None = None
    data_kind: str = "vector"


def _binding(
    field: str,
    *,
    property_name: str | None = None,
    value_status: str = "available",
    metric_key: str | None = None,
) -> AttributeBinding:
    return AttributeBinding(
        field=field,
        property_name=property_name or field,
        value_status=value_status,
        metric_key=metric_key,
    )


def _definition(
    layer_id: str,
    source_key: str,
    geometry_type: str,
    style_id: str,
    *,
    legend: tuple[Mapping[str, str], ...] = (),
    minimum_zoom: int = 0,
    attribute_bindings: tuple[AttributeBinding, ...] = (),
    optional: bool = False,
    verified_empty_statement: str | None = None,
    data_kind: str = "vector",
) -> LayerDefinition:
    return LayerDefinition(
        layer_id=layer_id,
        source_key=source_key,
        geometry_type=geometry_type,
        style_id=style_id,
        legend=legend,
        minimum_zoom=minimum_zoom,
        attribute_bindings=attribute_bindings,
        optional=optional,
        verified_empty_statement=verified_empty_statement,
        data_kind=data_kind,
    )


def _legend(label: str, value: str = "") -> dict[str, str]:
    entry = {"label": label}
    if value:
        entry["value"] = value
    return entry


_A_CLASS_DEFINITIONS: Mapping[str, tuple[LayerDefinition, ...]] = {
    "map.intensity": (
        _definition(
            "intensity-fusion",
            "product:intensity.fusion",
            "raster",
            "intensity-fusion",
            legend=(_legend("地震影响场", "融合烈度"),),
            attribute_bindings=(_binding("intensity", metric_key="intensity"),),
            data_kind="raster",
        ),
        _definition(
            "intensity-boundary",
            "shanghai.admin.city",
            "polygon",
            "intensity-boundary",
            legend=(_legend("行政区界"),),
        ),
    ),
    "map.economic_loss": (
        _definition(
            "economic-loss-town",
            "product:loss.economic",
            "polygon",
            "economic-loss-town",
            legend=(_legend("经济损失", "万元"),),
            attribute_bindings=(
                _binding("economic_loss", metric_key="total_loss_yuan"),
            ),
        ),
    ),
    "map.rescue_demand": (
        _definition(
            "rescue-demand-town",
            "product:loss.resources",
            "polygon",
            "rescue-demand-town",
            legend=(_legend("救援力量需求"),),
            attribute_bindings=(
                _binding("rescue_teams", metric_key="rescue_team.quantity"),
            ),
        ),
    ),
    "map.deaths": (
        _definition(
            "deaths-town",
            "product:loss.casualties",
            "polygon",
            "deaths-town",
            legend=(_legend("死亡人数"),),
            attribute_bindings=(_binding("deaths", metric_key="deaths"),),
        ),
    ),
    "map.injuries": (
        _definition(
            "injuries-town",
            "product:loss.casualties",
            "polygon",
            "injuries-town",
            legend=(_legend("受伤人数"),),
            attribute_bindings=(_binding("injuries", metric_key="injuries"),),
        ),
    ),
    "map.buried": (
        _definition(
            "buried-town",
            "product:loss.casualties",
            "polygon",
            "buried-town",
            legend=(_legend("压埋人数"),),
            attribute_bindings=(_binding("buried", metric_key="buried"),),
        ),
    ),
    "map.material_demand": (
        _definition(
            "material-demand-town",
            "product:loss.resources",
            "polygon",
            "material-demand-town",
            legend=(_legend("物资需求"),),
            attribute_bindings=(
                _binding("material_demand", metric_key="material_demand"),
            ),
        ),
    ),
    "map.gdp": (
        _definition(
            "gdp-raster",
            "shanghai.gdp.raster",
            "raster",
            "gdp-raster",
            legend=(_legend("GDP", "万元/km2"),),
            attribute_bindings=(_binding("gdp", metric_key="gdp"),),
            data_kind="raster",
        ),
    ),
    "map.transport": (
        _definition(
            "road-network",
            _TRANSPORT_OPTIONAL_ROAD,
            "line",
            "road-network",
            legend=(_legend("路网"),),
            attribute_bindings=(_binding("road_class"),),
            optional=True,
        ),
    ),
    "map.historical_earthquakes": (
        _definition(
            "historical-earthquakes",
            "shanghai.historical.earthquakes",
            "point",
            "historical-earthquakes",
            legend=(_legend("历史地震"),),
            attribute_bindings=(
                _binding("magnitude", metric_key="magnitude"),
                _binding("origin_time"),
            ),
            verified_empty_statement="检索范围内无历史地震记录",
        ),
    ),
    "map.population": (
        _definition(
            "population-town",
            "shanghai.population.town",
            "polygon",
            "population-town",
            legend=(_legend("人口", "人"),),
            attribute_bindings=(_binding("population", metric_key="population"),),
        ),
    ),
    "map.hazard_sources": (
        _definition(
            "hazard-sources",
            "shanghai.hazard_source",
            "point",
            "hazard-sources",
            legend=(_legend("危险源"),),
            attribute_bindings=(_binding("category"), _binding("risk_level")),
            verified_empty_statement="检索范围内无危险源记录",
        ),
    ),
    "map.schools": (
        _definition(
            "schools",
            "shanghai.education.school",
            "point",
            "schools",
            legend=(_legend("学校"),),
            attribute_bindings=(_binding("name"), _binding("capacity")),
        ),
    ),
    "map.hospitals": (
        _definition(
            "hospitals",
            "shanghai.health.hospital",
            "point",
            "hospitals",
            legend=(_legend("医院"),),
            attribute_bindings=(_binding("name"), _binding("beds")),
        ),
    ),
    "map.active_faults": (
        _definition(
            "active-faults",
            "shanghai.fault",
            "line",
            "active-faults",
            legend=(_legend("活动断裂"),),
            attribute_bindings=(_binding("name"), _binding("distance_km")),
            verified_empty_statement=_ACTIVE_FAULT_EMPTY_STATEMENT,
        ),
    ),
    "map.building_damage": (
        _definition(
            "building-damage-town",
            "product:loss.buildings",
            "polygon",
            "building-damage-town",
            legend=(_legend("房屋破坏"),),
            attribute_bindings=(
                _binding(
                    "damaged_buildings",
                    metric_key="severe_or_collapsed_area_m2",
                ),
            ),
        ),
        _definition(
            "building-town",
            "shanghai.building.town",
            "polygon",
            "building-town",
            legend=(_legend("房屋总量"),),
            attribute_bindings=(_binding("building_count"),),
        ),
    ),
    "map.key_targets": (
        _definition(
            "key-targets",
            "shanghai.key_target",
            "point",
            "key-targets",
            legend=(_legend("重要目标"),),
            attribute_bindings=(_binding("category"), _binding("criticality")),
            verified_empty_statement="检索范围内无重要目标记录",
        ),
        _definition(
            "lifeline",
            "shanghai.lifeline",
            "line",
            "lifeline",
            legend=(_legend("生命线"),),
            attribute_bindings=(_binding("category"),),
        ),
    ),
    "map.epicenter": (
        _definition(
            "epicenter",
            "event",
            "point",
            "epicenter",
            legend=(_legend("震中"),),
            attribute_bindings=(_binding("magnitude"), _binding("depth_km")),
        ),
        _definition(
            "admin-boundary",
            "shanghai.admin.city",
            "polygon",
            "admin-boundary",
            legend=(_legend("行政区界"),),
        ),
    ),
    "map.city_distances": (
        _definition(
            "distance-reference-points",
            "shanghai.distance.reference_points",
            "point",
            "distance-reference-points",
            legend=(_legend("主要城市"),),
            attribute_bindings=(_binding("name"), _binding("distance_km")),
        ),
    ),
}


def _to_map_layer(
    definition: LayerDefinition,
    context: Any,
) -> Any:
    from app.artifacts.renderers.map_renderer import MapLayer

    resolved_sources = getattr(context, "resolved_sources", None)
    if not isinstance(resolved_sources, Mapping):
        raise MapSourceUnavailableError(
            f"A-class map source resolution is required for {definition.source_key}"
        )
    source = resolved_sources.get(definition.source_key)
    if source is None:
        if definition.optional:
            return _missing_map_layer(definition)
        raise MapSourceUnavailableError(
            f"required map source is unavailable: {definition.source_key}"
        )
    source_status = str(
        source.get("status", "missing")
        if isinstance(source, Mapping)
        else getattr(source, "status", "missing")
    )
    if source_status == "missing":
        if definition.optional:
            return _missing_map_layer(definition)
        raise MapSourceUnavailableError(
            f"required map source is unavailable: {definition.source_key}"
        )
    metadata = {
        "layer_id": definition.layer_id,
        "source_key": definition.source_key,
        "geometry_type": definition.geometry_type,
        "style_id": definition.style_id,
        "legend": [dict(item) for item in definition.legend],
        "minimum_zoom": definition.minimum_zoom,
        "attribute_bindings": [
            item.to_dict() for item in definition.attribute_bindings
        ],
        "optional": definition.optional,
        "source_status": source_status,
        "data_kind": definition.data_kind,
        "verified_empty_statement": definition.verified_empty_statement,
        "verified_empty": source_status == "verified_empty",
        "source_checksum": (
            source.get("checksum")
            if isinstance(source, Mapping)
            else getattr(source, "checksum", None)
        ),
        "feature_count": (
            source.get("feature_count", 0)
            if isinstance(source, Mapping)
            else getattr(source, "feature_count", 0)
        ),
    }
    source_kind = (
        source.get("kind")
        if isinstance(source, Mapping)
        else getattr(source, "kind", None)
    )
    source_url = (
        source.get("url")
        if isinstance(source, Mapping)
        else getattr(source, "url", None)
    )
    source_payload = (
        source.get("source")
        if isinstance(source, Mapping)
        else getattr(source, "source", None)
    )
    source_style = (
        source.get("style")
        if isinstance(source, Mapping)
        else getattr(source, "style", None)
    )
    source_type = "raster" if source_kind == "raster" else "geojson"
    return MapLayer(
        id=definition.layer_id,
        url=str(source_url or f"local://inline/{definition.layer_id}"),
        type=source_type,
        source=dict(source_payload or {}),
        style=dict(source_style or {}),
        source_key=definition.source_key,
        metadata=metadata,
    )


def _missing_map_layer(definition: LayerDefinition) -> Any:
    from app.artifacts.renderers.map_renderer import MapLayer

    return MapLayer(
        id=definition.layer_id,
        url=f"local://inline/{definition.layer_id}",
        type="geojson",
        source={
            "type": "geojson",
            "data": {"type": "FeatureCollection", "features": []},
        },
        style={
            "type": "line"
            if definition.geometry_type in {"line", "multiline"}
            else "circle"
            if definition.geometry_type in {"point", "multipoint"}
            else "fill",
            "paint": {},
        },
        source_key=definition.source_key,
        metadata={
            "layer_id": definition.layer_id,
            "source_key": definition.source_key,
            "geometry_type": definition.geometry_type,
            "style_id": definition.style_id,
            "legend": [dict(item) for item in definition.legend],
            "minimum_zoom": definition.minimum_zoom,
            "attribute_bindings": [
                item.to_dict() for item in definition.attribute_bindings
            ],
            "optional": True,
            "source_status": "missing",
            "data_kind": definition.data_kind,
            "verified_empty": False,
        },
    )


class MapLayerRegistry:
    """Declarative A-class map layer registry."""

    @classmethod
    def artifact_keys(cls) -> tuple[str, ...]:
        return A_CLASS_ARTIFACTS

    @classmethod
    def definitions(cls, artifact_key: str) -> tuple[LayerDefinition, ...]:
        try:
            return _A_CLASS_DEFINITIONS[artifact_key]
        except KeyError as error:
            raise ValueError(f"unsupported A-class map artifact: {artifact_key}") from error

    @classmethod
    def build(cls, artifact_key: str, context: Any) -> tuple[Any, ...]:
        return tuple(
            _to_map_layer(definition, context)
            for definition in cls.definitions(artifact_key)
        )


def _catalog_quality(artifact_key: str) -> str:
    try:
        from app.artifacts.catalog import load_catalog

        definition = load_catalog(settings.artifact_catalog_path).get(
            artifact_key,
            "a3v-professional",
        )
        return definition.quality_policy
    except (FileNotFoundError, KeyError, ValueError):
        return "A"


def _layer_source_key(layer: Any) -> str:
    return str(
        getattr(layer, "source_key", None)
        or getattr(layer, "metadata", {}).get("source_key", "")
    )


def _metadata(layer: Any) -> Mapping[str, Any]:
    metadata = getattr(layer, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


class MapQualityPolicy:
    """Evaluate A-class map quality from declarative layers."""

    @classmethod
    def evaluate(cls, artifact_key: str, layers: Any) -> ArtifactQuality:
        grade = _catalog_quality(artifact_key)
        reasons: list[str] = []
        if not layers:
            return ArtifactQuality(
                grade=grade,
                needs_review=True,
                degradation_reasons=("no map layers",),
            )

        for layer in layers:
            metadata = _metadata(layer)
            source_key = _layer_source_key(layer)
            source_status = str(metadata.get("source_status", "bound"))
            optional = bool(metadata.get("optional", False))
            if metadata.get("placeholder"):
                reasons.append(f"{source_key} contains placeholder text")
            if metadata.get("unexpected_zero"):
                reasons.append(f"{source_key} contains an unexpected zero substitution")
            reasons.extend(_content_quality_reasons(source_key, layer))
            if source_status != "missing":
                continue
            if (
                artifact_key == "map.transport"
                and source_key == _TRANSPORT_OPTIONAL_ROAD
                and optional
            ):
                reasons.append("路网数据待复核")
                continue
            if (
                bool(metadata.get("verified_empty"))
                or source_status == "verified_empty"
            ) and source_key == "shanghai.fault":
                continue
            reasons.append(f"{source_key} is unavailable")

        return ArtifactQuality(
            grade=grade,
            needs_review=bool(reasons),
            degradation_reasons=tuple(dict.fromkeys(reasons)),
        )


def _content_quality_reasons(source_key: str, layer: Any) -> tuple[str, ...]:
    source = getattr(layer, "source", None)
    if not isinstance(source, Mapping):
        return ()
    data = source.get("data")
    if not isinstance(data, Mapping):
        return ()
    features = data.get("features")
    if not isinstance(features, list):
        return ()
    reasons: list[str] = []
    placeholders = {"available", "TBD", "TODO", "待补充", "稍后补充"}
    metadata = _metadata(layer)
    bound_properties = {
        str(binding.get("property_name") or binding.get("field") or "")
        for binding in metadata.get("attribute_bindings", ())
        if isinstance(binding, Mapping)
    }
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        value_status = str(properties.get("value_status") or "")
        if value_status == "rounded_to_zero":
            reasons.append(f"{source_key} contains a rounded-to-zero substitution")
        for property_name in bound_properties:
            value = properties.get(property_name)
            if (
                value_status in {"unavailable", "not_applicable"}
                and value is not None
            ):
                reasons.append(
                    f"{source_key} contains a value for unavailable metric "
                    f"{property_name}"
                )
            if (
                value_status == "available"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value == 0
            ):
                reasons.append(f"{source_key} contains an unexpected zero substitution")
        for key, value in properties.items():
            if key == "value_status":
                continue
            if isinstance(value, str) and value in placeholders:
                reasons.append(f"{source_key} contains placeholder text")
                break
    return tuple(dict.fromkeys(reasons))
