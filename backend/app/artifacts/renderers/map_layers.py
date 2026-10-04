from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.artifacts.renderers.base import RenderQuality
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

B_CLASS_ARTIFACTS = (
    "map.shelter_emergency",
    "map.pga_zoning",
    "map.reservoirs",
    "map.metro",
    "map.seismic_stations",
    "map.rescue_teams",
    "map.cultural_relics",
)

C_CLASS_ARTIFACTS = ("map.building_grid",)

_TRANSPORT_OPTIONAL_ROAD = "shanghai.road.network"
_ACTIVE_FAULT_EMPTY_STATEMENT = "检索范围内无活动断裂记录"
_B_CLASS_EMPTY_STATEMENT = "检索范围内无记录"
_B_CLASS_DEGRADE_NOTES = {
    "map.shelter_emergency": "疏散场地数据待复核",
    "map.pga_zoning": "区划数据待复核",
    "map.reservoirs": "水库数据待复核",
    "map.metro": "轨道交通数据待复核",
    "map.seismic_stations": "台站数据待复核",
    "map.rescue_teams": "救援队伍数据待复核",
    "map.cultural_relics": "文物数据待复核",
}
_C_CLASS_DEGRADE_NOTE = "模型分配，待复核"


class MapSourceUnavailableError(RuntimeError):
    """Raised when a required map source is missing or invalid."""


class RequiredDependencyMissingError(RuntimeError):
    """Raised when a conditional map cannot be produced from traceable inputs."""


@dataclass(frozen=True, slots=True)
class DegradeDecision:
    status: str
    needs_review: bool
    missing_assets: tuple[str, ...] = ()
    reason: str = ""
    spatialized_estimate: bool = False


@dataclass(frozen=True, slots=True)
class AttributeBinding:
    field: str
    property_name: str | None = None
    value_status: str = "available"
    metric_key: str | None = None
    allow_zero: bool = False

    def to_dict(self) -> dict[str, str | None]:
        return {
            "field": self.field,
            "property_name": self.property_name,
            "value_status": self.value_status,
            "metric_key": self.metric_key,
            "allow_zero": self.allow_zero,
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
    degradation_note: str | None = None


def _binding(
    field: str,
    *,
    property_name: str | None = None,
    value_status: str = "available",
    metric_key: str | None = None,
    allow_zero: bool = False,
) -> AttributeBinding:
    return AttributeBinding(
        field=field,
        property_name=property_name or field,
        value_status=value_status,
        metric_key=metric_key,
        allow_zero=allow_zero,
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
    degradation_note: str | None = None,
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
        degradation_note=degradation_note,
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
                _binding(
                    "rescue_teams",
                    metric_key="rescue_team.quantity",
                    allow_zero=True,
                ),
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
            attribute_bindings=tuple(
                _binding(
                    metric_key.split(".", 1)[0],
                    metric_key=metric_key,
                    allow_zero=True,
                )
                for metric_key in (
                    "tent.quantity",
                    "drinking_water.quantity",
                    "food.quantity",
                    "clothing.quantity",
                    "quilt.quantity",
                    "blanket.quantity",
                    "stretcher.quantity",
                    "sickbed.quantity",
                    "toilet.quantity",
                )
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
            "building-town",
            "shanghai.building.town",
            "polygon",
            "building-town",
            legend=(_legend("房屋总量"),),
            attribute_bindings=(_binding("building_count"),),
        ),
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
            attribute_bindings=(
                _binding("magnitude", allow_zero=True),
                _binding("depth_km", allow_zero=True),
            ),
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
            attribute_bindings=(
                _binding("name"),
                _binding("distance_km", allow_zero=True),
            ),
        ),
    ),
}


def _event_layer() -> LayerDefinition:
    return _definition(
        "epicenter",
        "event",
        "point",
        "epicenter",
        legend=(_legend("震中"),),
        attribute_bindings=(
            _binding("magnitude", allow_zero=True),
            _binding("depth_km", allow_zero=True),
        ),
    )


def _admin_city_layer() -> LayerDefinition:
    return _definition(
        "admin-boundary",
        "shanghai.admin.city",
        "polygon",
        "admin-boundary",
        legend=(_legend("行政区界"),),
    )


_B_CLASS_DEFINITIONS: Mapping[str, tuple[LayerDefinition, ...]] = {
    "map.shelter_emergency": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "shelter-emergency",
            "shanghai.shelter.emergency",
            "point",
            "shelter-emergency",
            legend=(_legend("疏散场地"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.shelter_emergency"],
        ),
    ),
    "map.pga_zoning": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "pga-zoning",
            "shanghai.pga.raster",
            "raster",
            "pga-zoning",
            legend=(_legend("地震动峰值加速度区划"),),
            optional=True,
            data_kind="raster",
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.pga_zoning"],
        ),
    ),
    "map.reservoirs": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "reservoirs",
            "shanghai.reservoir",
            "point",
            "reservoirs",
            legend=(_legend("水库"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.reservoirs"],
        ),
    ),
    "map.metro": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "metro",
            "shanghai.metro",
            "line",
            "metro",
            legend=(_legend("轨道交通"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.metro"],
        ),
    ),
    "map.seismic_stations": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "seismic-stations",
            "shanghai.seismic_station",
            "point",
            "seismic-stations",
            legend=(_legend("地震台站"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.seismic_stations"],
        ),
    ),
    "map.rescue_teams": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "rescue-teams",
            "shanghai.rescue_team",
            "point",
            "rescue-teams",
            legend=(_legend("救援队伍"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.rescue_teams"],
        ),
    ),
    "map.cultural_relics": (
        _event_layer(),
        _admin_city_layer(),
        _definition(
            "cultural-relics",
            "shanghai.cultural_relic",
            "point",
            "cultural-relics",
            legend=(_legend("文物单位"),),
            optional=True,
            verified_empty_statement=_B_CLASS_EMPTY_STATEMENT,
            degradation_note=_B_CLASS_DEGRADE_NOTES["map.cultural_relics"],
        ),
    ),
}


_C_CLASS_DEFINITIONS: Mapping[str, tuple[LayerDefinition, ...]] = {
    "map.building_grid": (
        _definition(
            "building-grid",
            "product:loss.buildings",
            "raster",
            "building-grid",
            legend=(_legend("建筑物公里格网"),),
            data_kind="raster",
        ),
        _definition(
            "admin-town-boundary",
            "shanghai.admin.town",
            "polygon",
            "admin-town-boundary",
            legend=(_legend("街镇边界"),),
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
    source_metadata = (
        source.get("metadata")
        if isinstance(source, Mapping)
        else getattr(source, "metadata", None)
    )
    source_metadata = source_metadata if isinstance(source_metadata, Mapping) else {}
    degradation_reason = definition.degradation_note or source_metadata.get(
        "degradation_reason"
    )
    if source_status == "missing":
        if definition.optional:
            return _missing_map_layer(definition, degradation_reason)
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
        "degradation_reason": degradation_reason,
        "source_checksum": (
            source.get("checksum")
            if isinstance(source, Mapping)
            else getattr(source, "checksum", None)
        ),
        "version": (
            source.get("version")
            if isinstance(source, Mapping)
            else getattr(source, "version", None)
        ),
        "feature_count": (
            source.get("feature_count", 0)
            if isinstance(source, Mapping)
            else getattr(source, "feature_count", 0)
        ),
        "spatialized_estimate": bool(
            source_metadata.get("spatialized_estimate", False)
        ),
        "allocation_rule": source_metadata.get("allocation_rule"),
        "input_checksum": source_metadata.get("input_checksum"),
        "allocation_inputs": source_metadata.get("allocation_inputs"),
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


def _missing_map_layer(
    definition: LayerDefinition,
    degradation_reason: Any = None,
) -> Any:
    from app.artifacts.renderers.map_renderer import MapLayer

    if not degradation_reason:
        degradation_reason = definition.degradation_note
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
            "degradation_reason": degradation_reason,
            "spatialized_estimate": False,
            "allocation_rule": None,
            "input_checksum": None,
            "allocation_inputs": None,
        },
    )


class MapLayerRegistry:
    """Declarative A/B/C-class map layer registry."""

    @classmethod
    def artifact_keys(cls) -> tuple[str, ...]:
        return A_CLASS_ARTIFACTS + B_CLASS_ARTIFACTS + C_CLASS_ARTIFACTS

    @classmethod
    def definitions(cls, artifact_key: str) -> tuple[LayerDefinition, ...]:
        if artifact_key in _A_CLASS_DEFINITIONS:
            return _A_CLASS_DEFINITIONS[artifact_key]
        if artifact_key in _B_CLASS_DEFINITIONS:
            return _B_CLASS_DEFINITIONS[artifact_key]
        if artifact_key in _C_CLASS_DEFINITIONS:
            return _C_CLASS_DEFINITIONS[artifact_key]
        raise ValueError(f"unsupported map artifact: {artifact_key}")

    @classmethod
    def build(cls, artifact_key: str, context: Any) -> tuple[Any, ...]:
        layers = tuple(
            _to_map_layer(definition, context)
            for definition in cls.definitions(artifact_key)
        )
        grade = _catalog_quality(artifact_key)
        if grade in {"B", "C"}:
            asset_resolutions = getattr(context, "resolved_sources", None)
            decision = MapDegradePolicy.evaluate(
                artifact_key,
                layers,
                asset_resolutions if isinstance(asset_resolutions, Mapping) else {},
            )
            if decision.status == "blocked":
                reason = decision.reason or (
                    f"{artifact_key} is missing a required dependency"
                )
                if grade == "B":
                    raise MapSourceUnavailableError(reason)
                raise RequiredDependencyMissingError(reason)
        return layers


class MapDegradePolicy:
    """Decide whether incomplete B/C maps may degrade, and why."""

    @classmethod
    def evaluate(
        cls,
        artifact_key: str,
        layers: Any,
        asset_resolutions: Any,
    ) -> DegradeDecision:
        grade = _catalog_quality(artifact_key)
        if grade == "B":
            return cls._evaluate_b(artifact_key, layers, asset_resolutions)
        if grade == "C":
            return cls._evaluate_c(artifact_key, asset_resolutions)
        return DegradeDecision(status="complete", needs_review=False)

    @classmethod
    def _evaluate_b(
        cls,
        artifact_key: str,
        layers: Any,
        asset_resolutions: Any,
    ) -> DegradeDecision:
        required = ("event", "shanghai.admin.city")
        missing_required = tuple(
            source_key
            for source_key in required
            if not cls._bound(asset_resolutions, source_key)
        )
        if missing_required:
            return DegradeDecision(
                status="blocked",
                needs_review=True,
                missing_assets=missing_required,
                reason=(
                    f"{artifact_key} requires the event point and administrative "
                    "boundary before optional data can degrade"
                ),
            )
        missing_optional = tuple(
            _layer_source_key(layer)
            for layer in layers
            if bool(_metadata(layer).get("optional", False))
            and str(_metadata(layer).get("source_status", "bound")) == "missing"
        )
        if missing_optional:
            return DegradeDecision(
                status="degraded",
                needs_review=True,
                missing_assets=missing_optional,
                reason=_B_CLASS_DEGRADE_NOTES.get(
                    artifact_key,
                    "可选数据待复核",
                ),
            )
        return DegradeDecision(status="complete", needs_review=False)

    @classmethod
    def _evaluate_c(
        cls,
        artifact_key: str,
        asset_resolutions: Any,
    ) -> DegradeDecision:
        missing: list[str] = []
        for source_key in ("shanghai.admin.town", "shanghai.building.town"):
            if not cls._bound(asset_resolutions, source_key):
                missing.append(source_key)
                continue
            checksum = cls._attr(asset_resolutions, source_key, "checksum")
            if not isinstance(checksum, str) or not checksum:
                missing.append(source_key)

        product = cls._value(asset_resolutions, "product:loss.buildings")
        product_metadata = cls._metadata_of(product)
        if not cls._bound(asset_resolutions, "product:loss.buildings"):
            missing.append("product:loss.buildings")
        else:
            if not bool(product_metadata.get("spatialized_estimate")):
                missing.append("product:loss.buildings")
            if not isinstance(
                product_metadata.get("allocation_rule"),
                str,
            ) or not product_metadata.get("allocation_rule"):
                missing.append("product:loss.buildings")
            if not isinstance(
                product_metadata.get("input_checksum"),
                str,
            ) or not product_metadata.get("input_checksum"):
                missing.append("product:loss.buildings")
            if not isinstance(
                cls._attr(asset_resolutions, "product:loss.buildings", "checksum"),
                str,
            ) or not cls._attr(
                asset_resolutions,
                "product:loss.buildings",
                "checksum",
            ):
                missing.append("product:loss.buildings")
            allocation_inputs = product_metadata.get("allocation_inputs")
            expected_inputs = {
                "loss.buildings": product_metadata.get("input_checksum"),
                "shanghai.building.town": cls._attr(
                    asset_resolutions,
                    "shanghai.building.town",
                    "checksum",
                ),
                "shanghai.admin.town": cls._attr(
                    asset_resolutions,
                    "shanghai.admin.town",
                    "checksum",
                ),
            }
            if not isinstance(allocation_inputs, Mapping):
                missing.append("product:loss.buildings")
            else:
                for input_key, expected_checksum in expected_inputs.items():
                    if (
                        not isinstance(expected_checksum, str)
                        or not expected_checksum
                        or allocation_inputs.get(input_key)
                        != expected_checksum
                    ):
                        missing.append("product:loss.buildings")
                        break

        if missing:
            unique = tuple(dict.fromkeys(missing))
            return DegradeDecision(
                status="blocked",
                needs_review=True,
                missing_assets=unique,
                reason=(
                    "map.building_grid requires a traceable spatialized building "
                    "model and every allocation-source input checksum"
                ),
            )
        return DegradeDecision(
            status="degraded",
            needs_review=True,
            reason=_C_CLASS_DEGRADE_NOTE,
            spatialized_estimate=True,
        )

    @staticmethod
    def _value(asset_resolutions: Any, source_key: str) -> Any:
        if not isinstance(asset_resolutions, Mapping):
            return None
        return asset_resolutions.get(source_key)

    @classmethod
    def _attr(
        cls,
        asset_resolutions: Any,
        source_key: str,
        name: str,
    ) -> Any:
        value = cls._value(asset_resolutions, source_key)
        if value is None:
            return None
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    @classmethod
    def _bound(cls, asset_resolutions: Any, source_key: str) -> bool:
        status = cls._attr(asset_resolutions, source_key, "status")
        return str(status or "missing") in {"bound", "verified_empty"}

    @staticmethod
    def _metadata_of(value: Any) -> Mapping[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            metadata = value.get("metadata")
            return metadata if isinstance(metadata, Mapping) else {}
        metadata = getattr(value, "metadata", None)
        return metadata if isinstance(metadata, Mapping) else {}


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
    """Evaluate A/B/C-class map quality from declarative layers."""

    @classmethod
    def evaluate(cls, artifact_key: str, layers: Any) -> RenderQuality:
        grade = _catalog_quality(artifact_key)
        reasons: list[str] = []
        missing_assets: list[str] = []
        spatialized_estimate = False
        if not layers:
            return RenderQuality(
                grade=grade,
                needs_review=True,
                degradation_reasons=("no map layers",),
            )

        for layer in layers:
            metadata = _metadata(layer)
            source_key = _layer_source_key(layer)
            source_status = str(metadata.get("source_status", "bound"))
            optional = bool(metadata.get("optional", False))
            spatialized_estimate = spatialized_estimate or bool(
                metadata.get("spatialized_estimate", False)
            )
            if metadata.get("placeholder"):
                reasons.append(f"{source_key} contains placeholder text")
            if metadata.get("unexpected_zero"):
                reasons.append(f"{source_key} contains an unexpected zero substitution")
            reasons.extend(_content_quality_reasons(source_key, layer))
            if source_status != "missing":
                continue
            missing_assets.append(source_key)
            if (
                artifact_key == "map.transport"
                and source_key == _TRANSPORT_OPTIONAL_ROAD
                and optional
            ):
                reasons.append(
                    str(metadata.get("degradation_reason") or "路网数据待复核")
                )
                continue
            if (
                bool(metadata.get("verified_empty"))
                or source_status == "verified_empty"
            ) and source_key == "shanghai.fault":
                continue
            if optional and metadata.get("degradation_reason"):
                reasons.append(str(metadata["degradation_reason"]))
                continue
            reasons.append(f"{source_key} is unavailable")

        if grade == "C":
            spatialized_estimate = True
            reasons.append(_C_CLASS_DEGRADE_NOTE)

        return RenderQuality(
            grade=grade,
            needs_review=bool(reasons),
            missing_assets=tuple(dict.fromkeys(missing_assets)),
            degradation_reasons=tuple(dict.fromkeys(reasons)),
            spatialized_estimate=spatialized_estimate,
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
    bindings = tuple(
        binding
        for binding in metadata.get("attribute_bindings", ())
        if isinstance(binding, Mapping)
    )
    bound_properties = {
        str(binding.get("property_name") or binding.get("field") or "")
        for binding in bindings
    }
    allow_zero = {
        str(binding.get("property_name") or binding.get("field") or ""): bool(
            binding.get("allow_zero")
        )
        for binding in bindings
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
                and not allow_zero.get(property_name, False)
            ):
                reasons.append(f"{source_key} contains an unexpected zero substitution")
        for key, value in properties.items():
            if key == "value_status":
                continue
            if isinstance(value, str) and value in placeholders:
                reasons.append(f"{source_key} contains placeholder text")
                break
    return tuple(dict.fromkeys(reasons))
