from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely import wkt as shapely_wkt
from shapely.geometry import Point, shape
from shapely.ops import nearest_points
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactTaskDependencyBinding, ProductionRun, ProductionTask
from app.artifacts.renderers.map_layers import LayerDefinition, MapLayerRegistry
from app.config import settings
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.raster_repository import load_raster_version
from app.data_assets.repository import AssetRecord, DataAssetRepository
from app.data_assets.models import DataAsset, DataAssetVersion
from app.data_assets.service import compute_table_checksum
from app.intensity.repository import IntensityRepository
from app.intensity.models import IntensityFieldProduct
from app.loss.domain import (
    LossMetricValueStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
)
from app.loss.models import LossMetricValue, LossProduct
from app.loss.repository import LossMetricValueWrite
from app.loss.service import recompute_product_checksum


class MapSourceResolutionError(RuntimeError):
    """Raised when a production map source cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ResolvedMapSource:
    source_key: str
    kind: str
    status: str
    url: str
    source: dict[str, Any]
    style: dict[str, Any]
    checksum: str | None
    version: str | None
    feature_count: int
    metadata: dict[str, Any]


_LOSS_PRODUCT_TYPES = {
    "loss.buildings": LossProductType.BUILDING_DAMAGE,
    "loss.population": LossProductType.POPULATION_IMPACT,
    "loss.casualties": LossProductType.CASUALTIES,
    "loss.economic": LossProductType.ECONOMIC_LOSS,
    "loss.resources": LossProductType.RESOURCE_DEMAND,
}

_ARTIFACT_METRIC_KEYS = {
    "map.economic_loss": ("total_loss_yuan",),
    "map.rescue_demand": ("rescue_team.quantity",),
    "map.deaths": ("deaths",),
    "map.injuries": ("injuries",),
    "map.buried": ("buried",),
    "map.building_damage": ("severe_or_collapsed_area_m2",),
}

def _haversine_km(left: Point, right: Point) -> float:
    lon1, lat1 = math.radians(left.x), math.radians(left.y)
    lon2, lat2 = math.radians(right.x), math.radians(right.y)
    delta_lon = lon2 - lon1
    delta_lat = lat2 - lat1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(value))


def _within_radius(geometry: Any, event: Any, radius_km: float) -> bool:
    if geometry is None:
        return False
    event_point = Point(
        float(getattr(event, "longitude")),
        float(getattr(event, "latitude")),
    )
    nearest = nearest_points(event_point, geometry)[1]
    return _haversine_km(event_point, nearest) <= radius_km


def _feature_from_record(record: AssetRecord) -> dict[str, Any] | None:
    if not record.geometry_wkt:
        return None
    geometry = shapely_wkt.loads(record.geometry_wkt)
    return {
        "type": "Feature",
        "geometry": geometry.__geo_interface__,
        "properties": dict(record.properties or {}),
    }


def _vector_source(
    source_key: str,
    features: list[dict[str, Any]],
    *,
    checksum: str | None,
    version: str | None,
    style: dict[str, Any],
    status: str = "bound",
) -> ResolvedMapSource:
    return ResolvedMapSource(
        source_key=source_key,
        kind="vector",
        status=status,
        url=f"local://inline/{source_key}",
        source={
            "type": "geojson",
            "data": {
                "type": "FeatureCollection",
                "features": features,
            },
        },
        style=style,
        checksum=checksum,
        version=version,
        feature_count=len(features),
        metadata={"verified_empty": status == "verified_empty"},
    )


def _write_png(
    values: np.ndarray,
    *,
    source_key: str,
    identity: uuid.UUID | None,
) -> tuple[str, np.ndarray]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise MapSourceResolutionError(
            f"raster source {source_key} contains no finite valid pixels"
        )
    else:
        minimum = float(np.nanmin(finite))
        maximum = float(np.nanmax(finite))
    spread = maximum - minimum
    if spread <= 0:
        normalized = np.full(values.shape, 128, dtype=np.uint8)
    else:
        normalized = np.clip(
            ((values - minimum) / spread) * 255,
            0,
            255,
        ).astype(np.uint8)
    image = Image.fromarray(normalized, mode="L")
    safe_key = "".join(character if character.isalnum() else "-" for character in source_key)
    suffix = identity.hex[:12] if identity is not None else uuid.uuid4().hex[:12]
    file_name = f"{safe_key}-{suffix}.png"
    target_dir = Path(settings.artifact_storage_root) / "artifact_maps"
    target_dir.mkdir(parents=True, exist_ok=True)
    image.save(target_dir / file_name, format="PNG")
    return f"local://artifact_maps/{file_name}", values


def _dataset_raster_source(
    payload: bytes,
    *,
    source_key: str,
    identity: uuid.UUID | None,
    checksum: str | None,
    version: str | None,
) -> ResolvedMapSource:
    with MemoryFile(payload).open() as dataset:
        if dataset.crs is None:
            raise MapSourceResolutionError(
                f"raster source {source_key} has no valid CRS"
            )
        values = dataset.read(1).astype(np.float64)
        nodata = dataset.nodata
        if nodata is not None:
            values = np.where(values == nodata, np.nan, values)
        if not np.isfinite(values).any():
            raise MapSourceResolutionError(
                f"raster source {source_key} has no finite valid pixels"
            )
        url, _ = _write_png(values, source_key=source_key, identity=identity)
        bounds = dataset.bounds
        if CRS.from_user_input(dataset.crs).to_epsg() == 4326:
            west, south, east, north = bounds
        else:
            west, south, east, north = Transformer.from_crs(
                dataset.crs,
                "EPSG:4326",
                always_xy=True,
            ).transform_bounds(*bounds)
    return ResolvedMapSource(
        source_key=source_key,
        kind="raster",
        status="bound",
        url=url,
        source={
            "type": "image",
            "url": url,
            "coordinates": [
                [west, north],
                [east, north],
                [east, south],
                [west, south],
            ],
        },
        style={"type": "raster", "paint": {"raster-opacity": 0.82}},
        checksum=checksum,
        version=version,
        feature_count=1,
        metadata={"verified_empty": False},
    )


def _array_raster_source(
    bands: list[np.ndarray],
    metadata: dict[str, Any],
    *,
    source_key: str,
    identity: uuid.UUID | None,
    checksum: str | None,
    version: str | None,
) -> ResolvedMapSource:
    if not bands:
        raise MapSourceResolutionError(f"{source_key} has no raster bands")
    array = np.asarray(bands[0], dtype=np.float64)
    srid_value = metadata.get("srid")
    if not isinstance(srid_value, int) or srid_value <= 0:
        raise MapSourceResolutionError(f"raster source {source_key} has invalid SRID")
    srid = int(srid_value)
    width = int(metadata.get("width") or array.shape[1])
    height = int(metadata.get("height") or array.shape[0])
    resolution = float(metadata.get("resolution_m") or 1000.0)
    origin_x = float(metadata.get("origin_x") or 0.0)
    origin_y = float(metadata.get("origin_y") or 0.0)
    transform = from_origin(origin_x, origin_y, resolution, resolution)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="float64",
            crs=CRS.from_epsg(srid),
            transform=transform,
            nodata=np.nan,
        ) as target:
            target.write(array, 1)
        return _dataset_raster_source(
            memory.read(),
            source_key=source_key,
            identity=identity,
            checksum=checksum,
            version=version,
        )


async def _loss_metric_features(
    product: LossProduct,
    metric_bindings: tuple[tuple[str, str], ...],
    area_records: list[AssetRecord],
    all_metric_rows: list[LossMetricValue],
) -> tuple[list[dict[str, Any]], str]:
    _verify_loss_product_checksum(product, all_metric_rows)
    metric_keys = tuple(key for key, _ in metric_bindings)
    rows = [
        row
        for row in all_metric_rows
        if row.value_type == "central" and row.metric_key in metric_keys
    ]
    by_area_metric: dict[tuple[str, str], list[LossMetricValue]] = {}
    for row in rows:
        by_area_metric.setdefault(
            (str(row.area_code), row.metric_key),
            [],
        ).append(row)
    features: list[dict[str, Any]] = []
    for record in area_records:
        feature = _feature_from_record(record)
        if feature is None:
            continue
        selected_rows: list[LossMetricValue] = []
        for metric_key, property_name in metric_bindings:
            rows_for_metric = by_area_metric.get(
                (str(record.business_key), metric_key),
                [],
            )
            if len(rows_for_metric) != 1:
                selected_rows = []
                break
            row = rows_for_metric[0]
            if row.numeric_value is None:
                selected_rows = []
                break
            feature["properties"][property_name] = float(row.numeric_value)
            selected_rows.append(row)
        if not selected_rows:
            continue
        feature["properties"]["value_status"] = _merged_value_status(
            [row.value_status for row in selected_rows]
        )
        feature["properties"]["product_checksum"] = product.output_checksum
        features.append(feature)
    status = "verified_empty" if not features else "bound"
    return features, status


def _metric_write_from_row(row: LossMetricValue) -> LossMetricValueWrite:
    numeric = row.numeric_value
    if numeric is not None:
        if row.metric_key.endswith(".quantity"):
            numeric = int(round(float(numeric)))
        else:
            numeric = float(numeric)
    return LossMetricValueWrite(
        area_scope=str(row.area_scope),
        area_code=str(row.area_code),
        area_name=row.area_name,
        metric_key=str(row.metric_key),
        value_type=LossValueType(row.value_type),
        value_status=LossMetricValueStatus(row.value_status),
        numeric_value=numeric,
        unit=str(row.unit),
        precision=row.precision,
        quality_grade=LossQualityGrade(row.quality_grade),
        note=row.note,
    )


def _verify_loss_product_checksum(
    product: LossProduct,
    rows: list[LossMetricValue],
) -> None:
    if not product.output_checksum:
        raise MapSourceResolutionError(
            f"loss product {product.product_type} has no output checksum"
        )
    metrics = tuple(_metric_write_from_row(row) for row in rows)
    computed = recompute_product_checksum(
        LossProductType(product.product_type),
        metrics,
        dict(product.statistics or {}),
    )
    if computed != product.output_checksum:
        raise MapSourceResolutionError(
            f"loss product {product.product_type} checksum mismatch"
        )


def _merged_value_status(statuses: list[str]) -> str:
    if any(status == "unavailable" for status in statuses):
        return "unavailable"
    if any(status == "not_applicable" for status in statuses):
        return "not_applicable"
    if any(status == "rounded_to_zero" for status in statuses):
        return "rounded_to_zero"
    if all(status == "zero" for status in statuses):
        return "zero"
    return "available"


class MapSourceResolver:
    def __init__(self, data_asset_repository: DataAssetRepository | None = None) -> None:
        self._data_asset_repository = data_asset_repository or DataAssetRepository()

    async def resolve(
        self,
        session: AsyncSession,
        *,
        task: ProductionTask,
        run: ProductionRun,
        event: Any,
        asset_versions: Any,
    ) -> dict[str, ResolvedMapSource]:
        from app.artifacts.catalog import load_catalog

        catalog = load_catalog(settings.artifact_catalog_path)
        definition = catalog.get(task.artifact_key, task.output_profile)
        resolved: dict[str, ResolvedMapSource] = {}

        bindings = (
            await session.scalars(
                select(ArtifactTaskDependencyBinding).where(
                    ArtifactTaskDependencyBinding.production_task_id == task.id
                )
            )
        ).all()
        bindings_by_key = {item.dependency_key: item for item in bindings}

        product_keys = {
            dependency.key
            for dependency in definition.depends_on
            if dependency.kind.value == "assessment_product"
        }
        needs_town_records = task.artifact_key in {
            "map.population",
            "map.building_damage",
        } or bool(
            product_keys
            & {"loss.casualties", "loss.economic", "loss.buildings"}
        )
        needs_city_records = "loss.resources" in product_keys

        town_asset = asset_versions.get("shanghai.admin.town")
        town_records: list[AssetRecord] = []
        if (
            needs_town_records
            and town_asset is not None
            and town_asset.resolution_status == "bound"
        ):
            town_records = await self._load_frozen_asset_records(
                session,
                "shanghai.admin.town",
                town_asset,
            )
        city_asset = asset_versions.get("shanghai.admin.city")
        city_records: list[AssetRecord] = []
        if (
            needs_city_records
            and city_asset is not None
            and city_asset.resolution_status == "bound"
        ):
            city_records = await self._load_frozen_asset_records(
                session,
                "shanghai.admin.city",
                city_asset,
            )

        registry_definitions = {
            item.source_key: item
            for item in MapLayerRegistry.definitions(task.artifact_key)
        }

        for source_key in definition.required_assets:
            if source_key.startswith("basemap."):
                continue
            layer_definition = registry_definitions.get(source_key) or LayerDefinition(
                layer_id=source_key,
                source_key=source_key,
                geometry_type="polygon",
                style_id=source_key,
            )
            item = asset_versions.get(source_key)
            if item is None or item.resolution_status != "bound":
                raise MapSourceResolutionError(
                    f"required asset {source_key} is unavailable"
                )
            resolved[source_key] = await self._asset_source(
                session,
                source_key=source_key,
                asset_item=item,
                layer_definition=layer_definition,
                event=event,
                town_records=town_records,
            )

        for source_key in definition.optional_assets:
            if source_key not in registry_definitions:
                continue
            layer_definition = registry_definitions[source_key]
            item = asset_versions.get(source_key)
            if item is None or item.resolution_status != "bound":
                resolved[source_key] = self._unavailable_source(
                    source_key,
                    layer_definition,
                )
                continue
            try:
                resolved[source_key] = await self._asset_source(
                    session,
                    source_key=source_key,
                    asset_item=item,
                    layer_definition=layer_definition,
                    event=event,
                    town_records=town_records,
                    optional=True,
                )
            except Exception as error:
                resolved[source_key] = self._unavailable_source(
                    source_key,
                    layer_definition,
                    reason=f"可选增强层 {source_key} 部分数据不可用，待复核: {error}",
                )

        for dependency in definition.depends_on:
            if dependency.kind.value != "assessment_product":
                continue
            binding = bindings_by_key.get(dependency.key)
            if binding is None or binding.resolution_status not in {"bound", "degraded"}:
                raise MapSourceResolutionError(
                    f"required assessment product {dependency.key} is unavailable"
                )
            source_key = f"product:{dependency.key}"
            layer_definition = registry_definitions.get(
                source_key,
                LayerDefinition(
                    layer_id=source_key,
                    source_key=source_key,
                    geometry_type="polygon",
                    style_id=source_key,
                ),
            )
            resolved[source_key] = await self._product_source(
                session,
                source_key=source_key,
                product_key=dependency.key,
                binding=binding,
                run=run,
                town_records=town_records,
                city_records=city_records,
                layer_definition=layer_definition,
                event=event,
                artifact_key=task.artifact_key,
            )

        for layer_definition in MapLayerRegistry.definitions(task.artifact_key):
            source_key = layer_definition.source_key
            if source_key == "event":
                resolved[source_key] = self._event_source(source_key, event)
                continue
            if source_key not in resolved:
                raise MapSourceResolutionError(
                    f"map layer source is not declared by catalog: {source_key}"
                )

        return resolved

    async def _load_frozen_asset_records(
        self,
        session: AsyncSession,
        source_key: str,
        asset_item: Any,
    ) -> list[AssetRecord]:
        version = await self._frozen_asset_version(session, asset_item, source_key)
        records = await self._data_asset_repository.list_records(session, version.id)
        self._verify_vector_content_checksum(version, records, source_key)
        return records

    def _event_source(self, source_key: str, event: Any) -> ResolvedMapSource:
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    float(getattr(event, "longitude")),
                    float(getattr(event, "latitude")),
                ],
            },
            "properties": {
                "magnitude": float(getattr(event, "magnitude")),
                "depth_km": float(getattr(event, "depth_km")),
                "value_status": "available",
            },
        }
        return _vector_source(
            source_key,
            [feature],
            checksum=None,
            version=None,
            style={
                "type": "circle",
                "paint": {
                    "circle-radius": 10,
                    "circle-color": "#b3261e",
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": 2,
                },
            },
        )

    async def _asset_source(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        asset_item: Any,
        layer_definition: LayerDefinition,
        event: Any,
        town_records: list[AssetRecord],
        optional: bool = False,
    ) -> ResolvedMapSource:
        version = await self._frozen_asset_version(session, asset_item, source_key)
        if layer_definition.data_kind == "raster":
            payload, _ = await load_raster_version(session, version.id)
            payload_checksum = hashlib.sha256(payload).hexdigest()
            if payload_checksum != version.checksum:
                raise MapSourceResolutionError(
                    f"raster asset {source_key} payload checksum mismatch"
                )
            return _dataset_raster_source(
                payload,
                source_key=source_key,
                identity=version.id,
                checksum=version.checksum,
                version=version.version,
            )
        records = await self._data_asset_repository.list_records(session, version.id)
        self._verify_vector_content_checksum(version, records, source_key)
        records, missing_join = self._join_geometry_records(
            source_key,
            records,
            town_records,
        )
        if missing_join and not optional:
            raise MapSourceResolutionError(
                f"asset {source_key} has {missing_join} records without "
                "usable geometry"
            )
        radius_filter = layer_definition.source_key in {
            "shanghai.fault",
            "shanghai.historical.earthquakes",
            "shanghai.hazard_source",
            "shanghai.key_target",
            "shanghai.distance.reference_points",
        }
        features, bad_records = self._filtered_features(
            records,
            event=event,
            radius_filter=radius_filter,
        )
        if bad_records:
            raise MapSourceResolutionError(
                f"asset {source_key} contains {bad_records} records with "
                "invalid geometry"
            )
        if not radius_filter and not features:
            raise MapSourceResolutionError(
                f"asset {source_key} has no renderable geometry"
            )
        if source_key == "shanghai.distance.reference_points":
            features = self._with_distances(features, event)
        status = "verified_empty" if not features else "bound"
        return _vector_source(
            source_key,
            features,
            checksum=version.checksum,
            version=version.version,
            style=_style_for_definition(layer_definition),
            status=status,
        )

    def _verify_vector_content_checksum(
        self,
        version: DataAssetVersion,
        records: list[AssetRecord],
        source_key: str,
    ) -> None:
        if not version.checksum:
            raise MapSourceResolutionError(
                f"frozen asset {source_key} has no content checksum"
            )
        summary = dict(version.schema_summary or {})
        columns = summary.get("columns")
        if not isinstance(columns, list) or not columns:
            columns = sorted(
                {key for record in records for key in (record.properties or {})}
            )
        normalized = NormalizedTableData(
            columns=tuple(str(column) for column in columns),
            records=tuple(
                NormalizedRecord(
                    row_number=record.row_number,
                    business_key=record.business_key,
                    properties=dict(record.properties or {}),
                    geometry_wkt=record.geometry_wkt,
                )
                for record in records
            ),
            source_crs=version.source_crs or "EPSG:4326",
            spatial_extent=None,
        )
        content_checksum = compute_table_checksum(normalized)
        if content_checksum != version.checksum:
            raise MapSourceResolutionError(
                f"asset {source_key} content checksum mismatch"
            )

    def _filtered_features(
        self,
        records: list[AssetRecord],
        *,
        event: Any,
        radius_filter: bool,
    ) -> tuple[list[dict[str, Any]], int]:
        features: list[dict[str, Any]] = []
        bad_records = 0
        for record in records:
            try:
                feature = _feature_from_record(record)
            except Exception:
                feature = None
            if feature is None:
                bad_records += 1
                continue
            if radius_filter and not _within_radius(
                shape(feature["geometry"]),
                event,
                50,
            ):
                continue
            features.append(feature)
        return features, bad_records

    async def _frozen_asset_version(
        self,
        session: AsyncSession,
        asset_item: Any,
        source_key: str,
    ) -> DataAssetVersion:
        version_id = getattr(asset_item, "asset_version_id", None)
        if version_id is None:
            raise MapSourceResolutionError(
                f"frozen asset {source_key} has no version identity"
            )
        row = (
            await session.execute(
                select(DataAssetVersion, DataAsset)
                .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
                .where(
                    DataAssetVersion.id == version_id,
                    DataAsset.asset_key == source_key,
                )
            )
        ).one_or_none()
        if row is None:
            raise MapSourceResolutionError(
                f"frozen asset version for {source_key} was not found"
            )
        version = row[0]
        frozen_checksum = getattr(asset_item, "checksum", None)
        if not isinstance(frozen_checksum, str) or not frozen_checksum:
            raise MapSourceResolutionError(
                f"frozen asset {source_key} has no checksum"
            )
        if version.checksum != frozen_checksum:
            raise MapSourceResolutionError(
                f"frozen checksum mismatch for asset {source_key}"
            )
        return version

    def _join_geometry_records(
        self,
        source_key: str,
        records: list[AssetRecord],
        town_records: list[AssetRecord],
    ) -> tuple[list[AssetRecord], int]:
        if any(record.geometry_wkt for record in records):
            return records, 0
        if source_key not in {"shanghai.population.town", "shanghai.building.town"}:
            return records, 0
        town_by_key = {record.business_key: record for record in town_records}
        joined: list[AssetRecord] = []
        missing = 0
        for record in records:
            town = town_by_key.get(record.business_key)
            if town is None or not town.geometry_wkt:
                missing += 1
                continue
            joined.append(
                AssetRecord(
                    row_number=record.row_number,
                    business_key=record.business_key,
                    properties=dict(record.properties or {}),
                    geometry_wkt=town.geometry_wkt,
                )
            )
        return joined, missing

    async def _product_source(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        product_key: str,
        binding: ArtifactTaskDependencyBinding,
        run: ProductionRun,
        town_records: list[AssetRecord],
        city_records: list[AssetRecord],
        layer_definition: LayerDefinition,
        event: Any,
        artifact_key: str,
    ) -> ResolvedMapSource:
        if product_key == "intensity.fusion":
            intensity_product = await session.get(
                IntensityFieldProduct,
                binding.bound_entity_id,
            )
            if (
                intensity_product is None
                or intensity_product.run_id != run.assessment_run_id
                or intensity_product.product_type != "fusion"
                or intensity_product.output_checksum != binding.bound_checksum
                or intensity_product.algorithm_version != binding.bound_version
            ):
                raise MapSourceResolutionError(
                    "intensity product binding identity mismatch"
                )
            bands, metadata = await IntensityRepository().load_raster(
                session,
                intensity_product.id,
            )
            return _array_raster_source(
                bands,
                metadata,
                source_key=source_key,
                identity=binding.bound_entity_id,
                checksum=binding.bound_checksum,
                version=binding.bound_version,
            )

        product_type = _LOSS_PRODUCT_TYPES.get(product_key)
        if product_type is None:
            raise MapSourceResolutionError(
                f"unsupported assessment product source: {product_key}"
            )
        product = await session.get(LossProduct, binding.bound_entity_id)
        if product is None:
            raise MapSourceResolutionError(f"loss product {product_key} is unavailable")
        if (
            product.run_id != run.assessment_run_id
            or product.product_type != product_type.value
            or product.output_checksum != binding.bound_checksum
            or product.algorithm_version != binding.bound_version
        ):
            raise MapSourceResolutionError(
                f"loss product binding identity mismatch for {product_key}"
            )
        if product.status not in {"complete", "partial"}:
            raise MapSourceResolutionError(f"loss product {product_key} is unavailable")
        metric_bindings = tuple(
            (binding.metric_key, binding.property_name or binding.field)
            for binding in layer_definition.attribute_bindings
            if binding.metric_key
        )
        if not metric_bindings:
            raise MapSourceResolutionError(
                f"map {artifact_key} has no product metric binding"
            )
        area_records = city_records if product_type is LossProductType.RESOURCE_DEMAND else town_records
        all_metric_rows = list(
            (
                await session.scalars(
                    select(LossMetricValue).where(
                        LossMetricValue.product_id == product.id
                    )
                )
            ).all()
        )
        features, status = await _loss_metric_features(
            product,
            metric_bindings,
            area_records,
            all_metric_rows,
        )
        if not features:
            raise MapSourceResolutionError(
                f"loss product {product_key} has no matching town metric values"
            )
        return _vector_source(
            source_key,
            features,
            checksum=product.output_checksum,
            version=binding.bound_version,
            style=_style_for_definition(layer_definition),
            status=status,
        )

    def _unavailable_source(
        self,
        source_key: str,
        layer_definition: LayerDefinition,
        *,
        reason: str | None = None,
    ) -> ResolvedMapSource:
        return ResolvedMapSource(
            source_key=source_key,
            kind="vector",
            status="missing",
            url=f"local://inline/{source_key}",
            source={"type": "geojson", "data": {"type": "FeatureCollection", "features": []}},
            style=_style_for_definition(layer_definition),
            checksum=None,
            version=None,
            feature_count=0,
            metadata={
                "verified_empty": False,
                "degradation_reason": reason,
            },
        )

    def _with_distances(
        self,
        features: list[dict[str, Any]],
        event: Any,
    ) -> list[dict[str, Any]]:
        event_point = Point(
            float(getattr(event, "longitude")),
            float(getattr(event, "latitude")),
        )
        for feature in features:
            geometry = shape(feature["geometry"])
            distance = _haversine_km(event_point, geometry.representative_point())
            feature["properties"]["distance_km"] = round(distance, 2)
            feature["properties"]["value_status"] = "available"
        return features


_METRIC_FILL_RANGES = {
    "deaths": (0.0, 1000.0),
    "injuries": (0.0, 1000.0),
    "buried": (0.0, 1000.0),
    "economic_loss": (0.0, 2_000_000.0),
    "rescue_teams": (0.0, 100.0),
    "damaged_buildings": (0.0, 10_000.0),
    "tent": (0.0, 100.0),
    "drinking_water": (0.0, 100.0),
    "food": (0.0, 100.0),
    "clothing": (0.0, 100.0),
    "quilt": (0.0, 100.0),
    "blanket": (0.0, 100.0),
    "stretcher": (0.0, 100.0),
    "sickbed": (0.0, 100.0),
    "toilet": (0.0, 100.0),
}
_METRIC_FILL_LOW = "#244c8c"
_METRIC_FILL_HIGH = "#d9261c"


def _metric_fill_property(definition: LayerDefinition) -> str | None:
    for binding in definition.attribute_bindings:
        if binding.metric_key:
            return binding.property_name or binding.field
    return None


def _style_for_definition(definition: LayerDefinition) -> dict[str, Any]:
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
    metric_property = _metric_fill_property(definition)
    if metric_property is not None:
        minimum, maximum = _METRIC_FILL_RANGES.get(
            metric_property,
            (0.0, 1.0),
        )
        fill_color = [
            "interpolate",
            ["linear"],
            ["get", metric_property],
            minimum,
            _METRIC_FILL_LOW,
            maximum,
            _METRIC_FILL_HIGH,
        ]
    else:
        fill_color = "#c77b3b"
    return {
        "type": "fill",
        "paint": {
            "fill-color": fill_color,
            "fill-opacity": 0.55,
        },
    }
