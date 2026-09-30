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
                _binding("economic_loss", metric_key="economic_loss"),
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
                _binding("rescue_teams", metric_key="rescue_teams"),
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
                _binding("damaged_buildings", metric_key="damaged_buildings"),
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


def _coordinate(context: Any, attribute: str, default: float) -> float:
    event = getattr(context, "event", None)
    value = getattr(event, attribute, None)
    if value is None:
        value = default
    return float(value)


def _feature_collection(definition: LayerDefinition, context: Any) -> dict[str, Any]:
    longitude = _coordinate(context, "longitude", 121.5)
    latitude = _coordinate(context, "latitude", 31.2)
    geometry_type = definition.geometry_type
    if geometry_type in {"point", "multipoint"}:
        geometry = {
            "type": "Point",
            "coordinates": [longitude, latitude],
        }
    elif geometry_type in {"line", "multiline"}:
        geometry = {
            "type": "LineString",
            "coordinates": [
                [longitude - 0.08, latitude - 0.04],
                [longitude, latitude + 0.04],
                [longitude + 0.08, latitude - 0.02],
            ],
        }
    else:
        geometry = {
            "type": "Polygon",
            "coordinates": [
                [
                    [longitude - 0.12, latitude - 0.08],
                    [longitude + 0.12, latitude - 0.08],
                    [longitude + 0.12, latitude + 0.08],
                    [longitude - 0.12, latitude + 0.08],
                    [longitude - 0.12, latitude - 0.08],
                ]
            ],
        }
    properties: dict[str, Any] = {
        "layer_id": definition.layer_id,
        "source_key": definition.source_key,
        "style_id": definition.style_id,
    }
    for binding in definition.attribute_bindings:
        properties[binding.property_name or binding.field] = binding.value_status
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": properties,
            }
        ],
    }


def _style_for(definition: LayerDefinition) -> dict[str, Any]:
    if definition.geometry_type in {"line", "multiline"}:
        return {
            "type": "line",
            "paint": {
                "line-color": "#b3261e",
                "line-width": 3,
                "line-opacity": 0.9,
            },
        }
    if definition.geometry_type in {"point", "multipoint"}:
        return {
            "type": "circle",
            "paint": {
                "circle-radius": 8,
                "circle-color": "#c5523f",
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": 2,
            },
        }
    return {
        "type": "fill",
        "paint": {
            "fill-color": "#c77b3b",
            "fill-opacity": 0.42,
        },
    }


def _asset_versions(context: Any) -> Mapping[str, Any]:
    value = getattr(context, "asset_versions", None)
    return value if isinstance(value, Mapping) else {}


def _asset_status(context: Any, source_key: str) -> str:
    asset_versions = _asset_versions(context)
    if not asset_versions:
        return "bound"
    item = asset_versions.get(source_key)
    if item is None:
        return "missing"
    status = getattr(item, "resolution_status", None)
    if status in {"bound", "missing"}:
        return str(status)
    return "bound"


def _product_status(context: Any, source_key: str) -> str:
    product_key = source_key.removeprefix("product:")
    bindings = getattr(context, "dependency_bindings", None)
    if bindings:
        for binding in bindings:
            if getattr(binding, "dependency_key", None) != product_key:
                continue
            if getattr(binding, "dependency_kind", None) != "assessment_product":
                continue
            status = getattr(binding, "resolution_status", None)
            if status in {"bound", "degraded"}:
                return str(status)
        return "missing"
    for attribute in ("product_values", "evaluation_products"):
        values = getattr(context, attribute, None)
        if isinstance(values, Mapping) and product_key in values:
            return "bound"
    return "bound" if not bindings else "missing"


def _source_status(context: Any, source_key: str) -> str:
    if source_key == "event":
        return "bound"
    if source_key.startswith("product:"):
        return _product_status(context, source_key)
    return _asset_status(context, source_key)


def _to_map_layer(
    definition: LayerDefinition,
    context: Any,
) -> Any:
    from app.artifacts.renderers.map_renderer import MapLayer

    source_status = _source_status(context, definition.source_key)
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
        "verified_empty": False,
    }
    return MapLayer(
        id=definition.layer_id,
        url=f"local://inline/{definition.layer_id}",
        type="geojson",
        source={
            "type": "geojson",
            "data": _feature_collection(definition, context),
        },
        style=_style_for(definition),
        source_key=definition.source_key,
        metadata=metadata,
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
