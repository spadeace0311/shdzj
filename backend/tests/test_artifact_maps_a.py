from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import numpy as np
from geoalchemy2.elements import WKTElement
from PIL import Image
from sqlalchemy import delete, select, update

from app.artifacts.basemap import MapViewportTileManifest, VIEWPORT_RADII_KM
from app.artifacts.context import ProductionContextService
from app.artifacts.renderers.map_layers import (
    MapLayerRegistry,
    MapQualityPolicy,
    MapSourceUnavailableError,
)
from app.artifacts.renderers.map_renderer import BrowserPool, MapRenderer, MapSpecBuilder
from app.artifacts.renderers.map_sources import MapSourceResolutionError
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.models import ArtifactTaskDependencyBinding
from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings
from app.db import engine
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import DataAsset, DataAssetRecord, DataAssetVersion
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.registry import get_asset_definition
from app.data_assets.repository import DataAssetRepository
from app.data_assets.service import DataAssetService, compute_table_checksum
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
from tests.basemap_fixtures import make_in_memory_package, png_tile_bytes
from tests.data_asset_helpers import (
    _population_records,
    _town_records,
)


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

LOSS_PRODUCT_TYPES = {
    "loss.buildings": "building_damage",
    "loss.population": "population_impact",
    "loss.casualties": "casualties",
    "loss.economic": "economic_loss",
    "loss.resources": "resource_demand",
}

_CREATED_VERSION_IDS: set[UUID] = set()


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_maps_a_data_assets(session_factory):
    yield
    version_ids = list(_CREATED_VERSION_IDS)
    _CREATED_VERSION_IDS.clear()
    if not version_ids:
        return
    from sqlalchemy import delete

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(DataAssetVersion).where(
                    DataAssetVersion.id.in_(version_ids)
                )
            )


def _settings_manifests() -> tuple[MapViewportTileManifest, ...]:
    return tuple(
        MapViewportTileManifest.build(
            center_lon=121.5,
            center_lat=31.2,
            radius_km=radius_km,
            output_width=settings.artifact_basemap_output_width,
            output_height=settings.artifact_basemap_output_height,
            zoom_levels=settings.artifact_basemap_zoom_levels,
            padding=settings.artifact_basemap_buffer_pixels,
        )
        for radius_km in VIEWPORT_RADII_KM
    )


@pytest.fixture
async def map_renderer(monkeypatch):
    package = make_in_memory_package(
        _settings_manifests(),
        provider="gaode",
        tile_bytes=png_tile_bytes(color=(70, 130, 180)),
    )
    package.checksum = "a" * 64
    monkeypatch.setattr(
        "app.artifacts.renderers.map_renderer.load_offline_basemap_candidates",
        lambda root: ({"gaode": package}, {}),
    )
    pool = BrowserPool(max_slots=1)
    await pool.start()
    yield MapRenderer(pool)
    await pool.close()


@pytest.fixture
async def production_renderer(monkeypatch):
    package = make_in_memory_package(
        _settings_manifests(),
        provider="gaode",
        tile_bytes=png_tile_bytes(color=(70, 130, 180)),
    )
    monkeypatch.setattr(
        "app.artifacts.renderers.map_renderer.load_offline_basemap_candidates",
        lambda root: ({"gaode": package}, {}),
    )
    pool = BrowserPool(max_slots=1)
    await pool.start()
    yield package, MapRenderer(pool)
    await pool.close()


async def _create_loss_product(
    fixture,
    *,
    product_type: str,
    version: str,
    checksum: str,
) -> LossProduct:
    async with fixture._session_factory() as session:
        async with session.begin():
            run = await session.get(AssessmentRun, fixture.assessment_run_id)
            assert run is not None
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == f"loss.{product_type}",
                )
            )
            if task is None:
                task = AssessmentTask(
                    run_id=run.id,
                    task_key=f"loss.{product_type}",
                    task_type="loss",
                    component="fixture",
                    sequence=1,
                    deadline_at=run.deadline_at,
                    status="succeeded",
                )
                session.add(task)
                await session.flush()
            product = LossProduct(
                run_id=run.id,
                task_id=task.id,
                product_type=product_type,
                status="complete",
                quality_grade="L1",
                calibration_status="calibrated",
                coverage_ratio=Decimal("1.000000"),
                partial_scope=False,
                needs_review=False,
                spatialized_estimate=False,
                algorithm_version=version,
                parameter_version="parameter-v1",
                region_profile_version="region-v1",
                input_fingerprint="c" * 64,
                input_checksum="d" * 64,
                output_checksum=checksum,
                statistics={},
            )
            session.add(product)
            await session.flush()
            return product


async def _add_loss_metric(
    fixture,
    product: LossProduct,
    *,
    metric_key: str,
    numeric_value: float,
    value_status: str,
    area_scope: str = "town",
    area_code: str = "310115000001",
    unit: str = "count",
) -> None:
    async with fixture._session_factory() as session:
        async with session.begin():
            session.add(
                LossMetricValue(
                    product_id=product.id,
                    area_scope=area_scope,
                    area_code=area_code,
                    area_name="fixture town",
                    metric_key=metric_key,
                    value_type="central",
                    value_status=value_status,
                    numeric_value=numeric_value,
                    unit=unit,
                    precision=2,
                    quality_grade="L1",
                )
            )


async def _prepare_product_dependencies(fixture, artifact_key: str):
    from app.artifacts.catalog import load_catalog

    definition = load_catalog(settings.artifact_catalog_path).get(
        artifact_key,
        "a3v-professional",
    )
    product_keys = {
        dependency.key
        for dependency in definition.depends_on
        if dependency.kind.value == "assessment_product"
    }
    if "intensity.fusion" in product_keys:
        await fixture.create_fusion_product()
    for product_key in sorted(product_keys & set(LOSS_PRODUCT_TYPES)):
        await _create_loss_product(
            fixture,
            product_type=LOSS_PRODUCT_TYPES[product_key],
            version=f"{product_key}-v1",
            checksum="e" * 64,
        )

    run = await fixture.create_full_run()
    task = await fixture.first_task(run.id, artifact_key)
    async with fixture._session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
    return task


async def _render_product_map(
    *,
    fixture,
    session_factory,
    production_renderer,
    tmp_path: Path,
    artifact_key: str,
    product_key: str,
    product_type: str,
    metrics: tuple[tuple[str, float, str, str], ...],
    publish_towns: bool,
) -> tuple:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    if publish_towns:
        await _publish_population_asset(
            session_factory,
            f"{artifact_key}-town-{uuid4()}",
        )
        if artifact_key == "map.building_damage":
            await _publish_asset(
                session_factory,
                "shanghai.building.town",
                records=_building_town_records(),
            )
    product = await _create_loss_product(
        fixture,
        product_type=product_type,
        version=f"{product_type}-{artifact_key}",
        checksum=_loss_checksum(product_type, metrics),
    )
    for metric_key, numeric_value, value_status, unit in metrics:
        await _add_loss_metric(
            fixture,
            product,
            metric_key=metric_key,
            numeric_value=numeric_value,
            value_status=value_status,
            area_scope="city" if product_type == "resource_demand" else "town",
            area_code="shanghai" if product_type == "resource_demand" else "310115000001",
            unit=unit,
        )

    run = await fixture.create_full_run()
    task = await fixture.first_task(run.id, artifact_key)
    async with session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                fixture.catalog,
            )
            context = await service.build_map_context(session, task.id)
    result = await renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / f"{artifact_key}.jpg",
    )
    return result, context


async def _replace_product_metrics_and_render(
    *,
    session_factory,
    production_renderer,
    tmp_path: Path,
    context,
    artifact_key: str,
    product_key: str,
    product_type: str,
    metrics: tuple[tuple[str, float, str, str], ...],
) -> tuple:
    binding = await _dependency_binding(
        session_factory,
        context.production_task_id,
        product_key,
    )
    area_scope = "city" if product_type == "resource_demand" else "town"
    area_code = "shanghai" if product_type == "resource_demand" else "310115000001"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(LossMetricValue).where(
                    LossMetricValue.product_id == binding.bound_entity_id
                )
            )
            for metric_key, numeric_value, value_status, unit in metrics:
                session.add(
                    LossMetricValue(
                        product_id=binding.bound_entity_id,
                        area_scope=area_scope,
                        area_code=area_code,
                        area_name="fixture town",
                        metric_key=metric_key,
                        value_type="central",
                        value_status=value_status,
                        numeric_value=numeric_value,
                        unit=unit,
                        precision=2,
                        quality_grade="L1",
                    )
                )
            product = await session.get(LossProduct, binding.bound_entity_id)
            assert product is not None
            product.output_checksum = _loss_checksum(product_type, metrics)
            await session.flush()
            await session.execute(
                update(ArtifactTaskDependencyBinding)
                .where(
                    ArtifactTaskDependencyBinding.production_task_id
                    == binding.production_task_id,
                    ArtifactTaskDependencyBinding.dependency_key == product_key,
                )
                .values(bound_checksum=product.output_checksum)
            )

    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    async with session_factory() as session:
        async with session.begin():
            high_context = await service.build_map_context(
                session,
                binding.production_task_id,
            )
    high_result = await renderer.render(
        MapSpecBuilder().build(high_context),
        tmp_path / f"{artifact_key}-high.jpg",
    )
    return high_result, high_context


async def _publish_asset(
    session_factory,
    asset_key: str,
    *,
    records: tuple[NormalizedRecord, ...] = (),
    spatial_extent: tuple[float, float, float, float] | None = None,
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=f"{asset_key}-maps-a-{datetime.now(UTC).timestamp()}",
                    source_uri="https://example.gov.invalid/maps-a",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="maps-a production fixture",
                    file_name=asset_key,
                    file_format="geojson",
                    file_size_bytes=1,
                    checksum="a" * 64,
                    relative_path=f"fixture/{asset_key}",
                    requested_by="maps-a-fixture",
                ),
            )
            normalized = NormalizedTableData(
                columns=tuple(
                    sorted({key for record in records for key in record.properties})
                ),
                records=records,
                source_crs="EPSG:4326",
                spatial_extent=spatial_extent,
            )
            await DataAssetService().populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "importer": "geojson",
                    "record_count": normalized.record_count,
                    "source_crs": normalized.source_crs,
                },
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor="maps-a-fixture",
            )
            if not report.publishable:
                raise AssertionError(
                    f"{asset_key} maps-a fixture failed validation: "
                    f"{[issue.message for issue in report.errors]}"
                )
            version = await service.publish_version(
                session,
                job.asset_version_id,
                "maps-a-fixture",
                "maps-a production fixture",
            )
            _CREATED_VERSION_IDS.add(version.id)
            return version.id


async def _publish_population_asset(session_factory, version: str) -> UUID:
    await _publish_asset(
        session_factory,
        "shanghai.admin.town",
        records=_town_records().records,
        spatial_extent=(121.4, 31.1, 121.6, 31.4),
    )
    return await _publish_asset(
        session_factory,
        "shanghai.population.town",
        records=_population_records().records,
    )


async def _active_region_bounds(
    session_factory,
) -> tuple[float, float, float, float]:
    from sqlalchemy import func

    from app.regions.models import RegionBoundary

    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    func.ST_XMin(RegionBoundary.geom),
                    func.ST_YMin(RegionBoundary.geom),
                    func.ST_XMax(RegionBoundary.geom),
                    func.ST_YMax(RegionBoundary.geom),
                )
                .where(RegionBoundary.is_active.is_(True))
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        raise AssertionError("active region boundary is required for GDP coverage")
    return tuple(float(value) for value in row)


def _write_publishable_gdp_raster(path: Path, bounds: tuple[float, ...]) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import Affine

    min_x, min_y, max_x, max_y = bounds
    width = 16
    height = 16
    transform = Affine(
        (max_x - min_x) / width,
        0,
        min_x,
        0,
        -(max_y - min_y) / height,
        max_y,
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        nodata=-9999.0,
        transform=transform,
    ) as target:
        target.write(np.full((height, width), 100.0, dtype="float32"), 1)


async def _publish_gdp_raster(
    session_factory,
    tmp_path: Path,
) -> DataAssetVersion:
    bounds = await _active_region_bounds(session_factory)
    source_path = tmp_path / "gdp-publishable.tif"
    _write_publishable_gdp_raster(source_path, bounds)
    definition = get_asset_definition("shanghai.gdp.raster")
    descriptor = GeoTiffAssetImporter().load(source_path, definition)
    source_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()

    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key="shanghai.gdp.raster",
                    version=f"gdp-publishable-{uuid4()}",
                    source_uri="https://example.gov.invalid/gdp-publishable",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="maps-a publishable GDP fixture",
                    file_name="gdp-publishable.tif",
                    file_format="geotiff",
                    file_size_bytes=source_path.stat().st_size,
                    checksum=source_checksum,
                    relative_path="fixture/gdp-publishable.tif",
                    requested_by="maps-a-fixture",
                ),
            )
            service = DataAssetService()
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                descriptor,
                {
                    "file_format": "geotiff",
                    "source_crs": descriptor.source_crs,
                },
                source_path=source_path,
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor="maps-a-fixture",
            )
            if not report.publishable:
                raise AssertionError(
                    "GDP raster fixture failed validation: "
                    f"{[issue.message for issue in report.errors]}"
                )
            version = await service.publish_version(
                session,
                job.asset_version_id,
                "maps-a-fixture",
                "maps-a publishable GDP fixture",
            )
            _CREATED_VERSION_IDS.add(version.id)
            return version


def _loss_checksum(
    product_type: str,
    metrics: tuple[tuple[str, float, str, str], ...],
) -> str:
    writes = tuple(
        LossMetricValueWrite(
            area_scope="city" if product_type == "resource_demand" else "town",
            area_code="shanghai" if product_type == "resource_demand" else "310115000001",
            area_name="fixture town",
            metric_key=metric_key,
            value_type=LossValueType.CENTRAL,
            value_status=LossMetricValueStatus(value_status),
            numeric_value=(
                int(round(numeric_value))
                if metric_key.endswith(".quantity")
                else float(numeric_value)
            ),
            unit=unit,
            precision=2,
            quality_grade=LossQualityGrade.L1,
            note=None,
        )
        for metric_key, numeric_value, value_status, unit in metrics
    )
    return recompute_product_checksum(
        LossProductType(product_type),
        writes,
        {},
    )


async def _dependency_binding(
    session_factory,
    task_id: UUID,
    product_key: str,
) -> ArtifactTaskDependencyBinding:
    async with session_factory() as session:
        binding = await session.scalar(
            select(ArtifactTaskDependencyBinding).where(
                ArtifactTaskDependencyBinding.production_task_id == task_id,
                ArtifactTaskDependencyBinding.dependency_key == product_key,
            )
        )
        assert binding is not None
        return binding


def _admin_city_record() -> NormalizedRecord:
    return NormalizedRecord(
        row_number=1,
        business_key="shanghai",
        properties={"ID": "shanghai", "NAME": "Shanghai"},
        geometry_wkt=(
            "MULTIPOLYGON (((121.2 30.9, 121.8 30.9, "
            "121.8 31.5, 121.2 31.5, 121.2 30.9)))"
        ),
    )


def _far_fault_record() -> NormalizedRecord:
    return NormalizedRecord(
        row_number=1,
        business_key="1",
        properties={"OBJECTID": 1, "name": "far fault", "LENGTH": 100.0},
        geometry_wkt="MULTILINESTRING ((126.0 36.0, 126.6 36.6))",
    )


def _admin_city_records() -> tuple[NormalizedRecord, ...]:
    return (
        _admin_city_record(),
        NormalizedRecord(
            row_number=2,
            business_key="shanghai-2",
            properties={"ID": "shanghai-2", "NAME": "Shanghai East"},
            geometry_wkt=(
                "MULTIPOLYGON (((121.2 30.9, 121.8 30.9, "
                "121.8 31.5, 121.2 31.5, 121.2 30.9)))"
            ),
        ),
    )


def _building_town_records() -> tuple[NormalizedRecord, ...]:
    return tuple(
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "id": town_code,
                "name": f"town-{index}",
                "TOTAL_AREA": 1000,
                "HIGH_RISE": 200,
                "RCFRAME": 300,
                "BRICK_STRUCTURE": 250,
                "SINGLE_AREA": 150,
                "OTHER_STRUCTURE": 100,
            },
            geometry_wkt=None,
        )
        for index, town_code in enumerate(
            (f"{310115000001 + offset}" for offset in range(212)),
            start=1,
        )
    )


def _road_records() -> tuple[NormalizedRecord, ...]:
    return tuple(
        NormalizedRecord(
            row_number=index,
            business_key=str(index),
            properties={"road_class": "primary"},
            geometry_wkt=(
                f"MULTILINESTRING ((121.{index} 31.{index}, "
                f"121.{index + 1} 31.{index + 1}))"
            ),
        )
        for index in (1, 2)
    )


async def _publish_optional_road_network(
    session_factory,
    *,
    corrupt: bool = False,
) -> UUID:
    records = _road_records()
    normalized = NormalizedTableData(
        columns=tuple(sorted({key for record in records for key in record.properties})),
        records=records,
        source_crs="EPSG:4326",
        spatial_extent=(121.1, 31.1, 121.3, 31.3),
    )
    checksum = compute_table_checksum(normalized)
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(DataAssetVersion)
                .where(
                    DataAssetVersion.asset_id.in_(
                        select(DataAsset.id).where(
                            DataAsset.asset_key == "shanghai.road.network",
                            DataAsset.region_id == "shanghai",
                        )
                    ),
                    DataAssetVersion.status == "published",
                )
                .values(status="retired", retired_at=now)
            )
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == "shanghai.road.network",
                    DataAsset.region_id == "shanghai",
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key="shanghai.road.network",
                    region_id="shanghai",
                    name="上海市路网",
                    data_type="vector",
                    spatial_granularity="feature",
                    responsibility_unit="fixture",
                    update_interval_days=365,
                    is_core=False,
                    contract={"business_key_fields": [], "fields": []},
                )
                session.add(asset)
                await session.flush()
            version = DataAssetVersion(
                asset_id=asset.id,
                version=f"road-network-maps-a-{uuid4()}",
                status="imported",
                source_uri="https://example.gov.invalid/road-network",
                source_crs="EPSG:4326",
                checksum=checksum,
                schema_summary={
                    "columns": list(normalized.columns),
                    "normalized_checksum": checksum,
                },
                imported_by="maps-a-fixture",
                imported_at=now,
                published_at=now,
            )
            session.add(version)
            await session.flush()
            for record in records:
                geometry = (
                    None
                    if corrupt and record.business_key == "2"
                    else WKTElement(record.geometry_wkt, srid=4326)
                )
                session.add(
                    DataAssetRecord(
                        version_id=version.id,
                        row_number=record.row_number,
                        business_key=record.business_key,
                        properties=dict(record.properties),
                        geom=geometry,
                    )
                )
            await session.flush()
            persisted_records = await DataAssetRepository().list_records(
                session,
                version.id,
            )
            persisted_checksum = compute_table_checksum(
                NormalizedTableData(
                    columns=normalized.columns,
                    records=tuple(
                        NormalizedRecord(
                            row_number=item.row_number,
                            business_key=item.business_key,
                            properties=dict(item.properties),
                            geometry_wkt=item.geometry_wkt,
                        )
                        for item in persisted_records
                    ),
                    source_crs="EPSG:4326",
                    spatial_extent=None,
                )
            )
            version.checksum = persisted_checksum
            version.schema_summary = {
                "columns": list(normalized.columns),
                "normalized_checksum": persisted_checksum,
            }
            version.status = "validated"
            version.validated_at = now
            await session.flush()
            version.status = "published"
            version.published_at = now
            await session.flush()
            _CREATED_VERSION_IDS.add(version.id)
            return version.id


async def _publish_partially_corrupt_admin_city(session_factory) -> UUID:
    definition = get_asset_definition("shanghai.admin.city")
    records = _admin_city_records()
    normalized = NormalizedTableData(
        columns=tuple(sorted({key for record in records for key in record.properties})),
        records=records,
        source_crs="EPSG:4326",
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(DataAssetVersion)
                .where(
                    DataAssetVersion.asset_id.in_(
                        select(DataAsset.id).where(
                            DataAsset.asset_key == "shanghai.admin.city",
                            DataAsset.region_id == definition.region_id,
                        )
                    ),
                    DataAssetVersion.status == "published",
                )
                .values(status="retired", retired_at=now)
            )
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == definition.asset_key,
                    DataAsset.region_id == definition.region_id,
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=definition.asset_key,
                    region_id=definition.region_id,
                    name=definition.name,
                    data_type=definition.data_type.value,
                    spatial_granularity=definition.spatial_granularity,
                    responsibility_unit="fixture",
                    update_interval_days=definition.update_interval_days,
                    is_core=definition.is_core,
                    contract={},
                )
                session.add(asset)
                await session.flush()
            version = DataAssetVersion(
                asset_id=asset.id,
                version=f"admin-city-corrupt-{uuid4()}",
                status="imported",
                source_uri="https://example.gov.invalid/admin-city-corrupt",
                source_crs="EPSG:4326",
                checksum="",
                schema_summary={"columns": list(normalized.columns)},
                imported_by="maps-a-fixture",
                imported_at=now,
                published_at=now,
            )
            session.add(version)
            await session.flush()
            for index, record in enumerate(records):
                session.add(
                    DataAssetRecord(
                        version_id=version.id,
                        row_number=record.row_number,
                        business_key=record.business_key,
                        properties=dict(record.properties),
                        geom=(
                            None
                            if index == 1
                            else WKTElement(record.geometry_wkt, srid=4326)
                        ),
                    )
                )
            await session.flush()
            persisted_records = await DataAssetRepository().list_records(
                session,
                version.id,
            )
            checksum = compute_table_checksum(
                NormalizedTableData(
                    columns=normalized.columns,
                    records=tuple(
                        NormalizedRecord(
                            row_number=item.row_number,
                            business_key=item.business_key,
                            properties=dict(item.properties),
                            geometry_wkt=item.geometry_wkt,
                        )
                        for item in persisted_records
                    ),
                    source_crs="EPSG:4326",
                    spatial_extent=None,
                )
            )
            version.checksum = checksum
            version.schema_summary = {
                "columns": list(normalized.columns),
                "normalized_checksum": checksum,
            }
            version.status = "validated"
            version.validated_at = now
            await session.flush()
            version.status = "published"
            version.published_at = now
            await session.flush()
            _CREATED_VERSION_IDS.add(version.id)
            return version.id


def _raster_source(source_key: str, tmp_path: Path) -> dict:
    target_dir = Path(settings.artifact_storage_root) / "artifact_maps"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{source_key}-fixture.png"
    Image.new("L", (8, 8), 90).save(target_dir / file_name, format="PNG")
    return {
        "source_key": source_key,
        "kind": "raster",
        "status": "bound",
        "url": f"local://artifact_maps/{file_name}",
        "source": {
            "type": "image",
            "url": f"local://artifact_maps/{file_name}",
            "coordinates": [
                [121.0, 31.5],
                [122.0, 31.5],
                [122.0, 30.8],
                [121.0, 30.8],
            ],
        },
        "style": {"type": "raster", "paint": {"raster-opacity": 0.8}},
        "checksum": None,
        "feature_count": 1,
        "metadata": {"verified_empty": False},
    }


def _verified_empty_source(source_key: str) -> dict:
    return {
        "source_key": source_key,
        "kind": "vector",
        "status": "verified_empty",
        "url": f"local://inline/{source_key}",
        "source": {"type": "geojson", "data": {"type": "FeatureCollection", "features": []}},
        "style": {
            "type": "line",
            "paint": {"line-color": "#b3261e", "line-width": 3},
        },
        "checksum": None,
        "feature_count": 0,
        "metadata": {"verified_empty": True},
    }


async def test_registry_rejects_missing_required_resolved_source(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.gdp")
    context = replace(context, resolved_sources={})

    with pytest.raises(MapSourceUnavailableError):
        MapLayerRegistry.build("map.gdp", context)


def test_registry_builds_real_raster_source(tmp_path):
    sources = {
        definition.source_key: _raster_source(definition.source_key, tmp_path)
        for definition in MapLayerRegistry.definitions("map.gdp")
    }
    layers = MapLayerRegistry.build(
        "map.gdp",
        type("Context", (), {"resolved_sources": sources})(),
    )

    raster_layer = next(layer for layer in layers if layer.id == "gdp-raster")
    assert raster_layer.type == "raster"
    assert raster_layer.source["type"] == "image"
    assert raster_layer.url.startswith("local://artifact_maps/")


async def test_active_fault_verified_empty_is_allowed(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.active_faults")
    sources = {
        definition.source_key: _verified_empty_source(definition.source_key)
        for definition in MapLayerRegistry.definitions("map.active_faults")
    }
    context = replace(context, resolved_sources=sources)
    layers = MapLayerRegistry.build("map.active_faults", context)
    quality = MapQualityPolicy.evaluate("map.active_faults", layers)
    spec = MapSpecBuilder().build(context)

    assert quality.grade == "A"
    assert quality.needs_review is False
    assert "检索范围内无活动断裂记录" in spec.source_notes


def test_quality_policy_detects_placeholder_and_unexpected_zero() -> None:
    layer = type(
        "Layer",
        (),
        {
            "source_key": "product:loss.economic",
            "metadata": {
                "source_status": "bound",
                "attribute_bindings": [
                    {
                        "field": "economic_loss",
                        "property_name": "economic_loss",
                    }
                ],
            },
            "source": {
                "type": "geojson",
                "data": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [121.5, 31.2]},
                            "properties": {
                                "economic_loss": 0,
                                "value_status": "available",
                            },
                        },
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [121.5, 31.2]},
                            "properties": {"name": "TBD"},
                        },
                    ],
                },
            },
        },
    )()
    quality = MapQualityPolicy.evaluate("map.economic_loss", (layer,))

    assert quality.needs_review is True
    assert any("unexpected zero" in reason for reason in quality.degradation_reasons)
    assert any("placeholder" in reason for reason in quality.degradation_reasons)


async def test_production_mode_marker_is_rendered_and_named(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    context = replace(
        context,
        production_mode="test",
        marker="【测试】",
    )
    result = await map_renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / "test-epicenter.jpg",
    )

    assert result.file_name.startswith("【测试】")
    assert result.render_manifest["marker"] == "【测试】"
    with Image.open(result.path) as image:
        marker_text = image.info.get("comment", b"").decode("utf-8")
    assert marker_text == "【测试】"


def _has_red_marker(path: Path) -> bool:
    with Image.open(path).convert("RGB") as image:
        region = image.crop((180, 450, 800, 780))
        pixels = list(region.getdata())
    return any(
        red > 140 and green < 90 and blue < 90
        for red, green, blue in pixels
    )


def _central_map_red_mean(path: Path) -> float:
    with Image.open(path).convert("RGB") as image:
        region = image.crop((1600, 1000, 3200, 2200))
        pixels = np.asarray(region, dtype=np.uint8)
    return float(pixels[:, :, 0].mean())


async def _page_text(
    map_renderer,
    spec,
    selector: str,
) -> str:
    slot = await map_renderer._browser_pool.acquire_page()
    try:
        await slot.page.set_content(
            map_renderer._renderer_html(spec, {}),
            wait_until="domcontentloaded",
        )
        return await slot.page.locator(selector).inner_text()
    finally:
        await map_renderer._browser_pool.release_slot(slot.token)


@pytest.mark.parametrize(
    ("production_mode", "marker"),
    (
        ("test", "【测试】"),
        ("drill", "【演练】"),
        ("replay", "【测试回放】"),
    ),
)
async def test_mode_markers_are_visible_in_pixels(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path: Path,
    production_mode,
    marker,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    context = replace(
        context,
        production_mode=production_mode,
        marker=marker,
    )
    result = await map_renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / f"{production_mode}.jpg",
    )

    assert result.file_name.startswith(marker)
    assert _has_red_marker(result.path) is True
    page_text = await _page_text(
        map_renderer,
        MapSpecBuilder().build(context),
        ".mode-marker",
    )
    assert page_text == marker


async def test_review_marker_is_visible_on_page(
    seeded_artifact_assessment,
    map_renderer,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.transport")
    context = replace(
        context,
        resolved_sources={
            "shanghai.road.network": {
                "source_key": "shanghai.road.network",
                "kind": "vector",
                "status": "missing",
                "url": "local://inline/shanghai.road.network",
                "source": {
                    "type": "geojson",
                    "data": {"type": "FeatureCollection", "features": []},
                },
                "style": {"type": "line", "paint": {}},
                "checksum": None,
                "version": None,
                "feature_count": 0,
                "metadata": {"verified_empty": False},
            }
        },
    )
    spec = MapSpecBuilder().build(context)
    page_text = await _page_text(map_renderer, spec, ".quality")
    assert "待复核" in page_text


async def test_production_population_context_resolves_published_vector(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await _publish_population_asset(
        session_factory,
        f"maps-a-{id(seeded_artifact_assessment)}",
    )
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.population")

    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    layers = MapLayerRegistry.build("map.population", context)
    population_layer = next(
        layer for layer in layers if layer.id == "population-town"
    )
    features = population_layer.source["data"]["features"]
    assert features
    assert features[0]["properties"]["total"] == 100
    assert population_layer.metadata["source_checksum"]


async def test_production_gdp_context_resolves_published_raster(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    published_version = await _publish_gdp_raster(session_factory, tmp_path)

    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.gdp")
    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    layers = MapLayerRegistry.build("map.gdp", context)
    raster_layer = next(layer for layer in layers if layer.id == "gdp-raster")
    assert raster_layer.type == "raster"
    assert raster_layer.source["type"] == "image"
    frozen = context.asset_versions["shanghai.gdp.raster"]
    assert frozen.version == published_version.version
    assert frozen.checksum == published_version.checksum
    result = await renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / "gdp.jpg",
    )
    assert result.non_empty_ratio > 0.2
    assert result.render_manifest["layer_versions"]["gdp-raster"] == frozen.version
    assert result.render_manifest["layer_checksums"]["gdp-raster"] == frozen.checksum


async def test_real_missing_required_asset_fails_hard(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.epicenter")

    published_ids = []
    async with session_factory() as session:
        async with session.begin():
            from app.data_assets.models import DataAsset

            rows = (
                await session.scalars(
                    select(DataAssetVersion)
                    .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
                    .where(
                        DataAsset.asset_key == "shanghai.admin.city",
                        DataAssetVersion.status == "published",
                    )
                )
            ).all()
            published_ids = [row.id for row in rows]
            for row in rows:
                row.status = "retired"
                row.retired_at = datetime.now(UTC)

    try:
        async with session_factory() as session:
            async with session.begin():
                await service.freeze_static_context(
                    session,
                    run.id,
                    seeded_artifact_assessment.catalog,
                )
                with pytest.raises(MapSourceResolutionError):
                    await service.build_map_context(session, task.id)
    finally:
        async with session_factory() as session:
            async with session.begin():
                for version_id in published_ids:
                    version = await session.get(DataAssetVersion, version_id)
                    if version is not None:
                        version.status = "published"
                        version.retired_at = None
                        version.published_at = datetime.now(UTC)


async def test_real_verified_empty_fault_source(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    await _publish_asset(
        session_factory,
        "shanghai.fault",
        records=(_far_fault_record(),),
        spatial_extent=(126.0, 36.0, 126.6, 36.6),
    )
    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.active_faults")
    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    layers = MapLayerRegistry.build("map.active_faults", context)
    fault_layer = next(layer for layer in layers if layer.id == "active-faults")
    assert fault_layer.metadata["verified_empty"] is True
    spec = MapSpecBuilder().build(context)
    assert "检索范围内无活动断裂记录" in spec.source_notes
    page_text = await _page_text(renderer, spec, ".footer")
    assert "检索范围内无活动断裂记录" in page_text
    result = await renderer.render(
        spec,
        tmp_path / "fault-empty.jpg",
    )
    assert result.non_empty_ratio > 0.2
    assert result.quality.grade == "A"


async def test_required_asset_partial_corruption_fails_hard(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await _publish_partially_corrupt_admin_city(session_factory)

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.epicenter")
    with pytest.raises(MapSourceResolutionError):
        async with session_factory() as session:
            async with session.begin():
                await service.freeze_static_context(
                    session,
                    run.id,
                    seeded_artifact_assessment.catalog,
                )
                await service.build_map_context(session, task.id)


async def test_optional_asset_partial_corruption_degrades(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    await _publish_optional_road_network(session_factory, corrupt=True)

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.transport")
    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    layers = MapLayerRegistry.build("map.transport", context)
    road_layer = next(layer for layer in layers if layer.id == "road-network")
    assert road_layer.metadata["source_status"] == "missing"
    quality = MapQualityPolicy.evaluate("map.transport", layers)
    assert quality.needs_review is True
    assert any("待复核" in reason for reason in quality.degradation_reasons)


async def test_loss_product_version_mismatch_fails_closed(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    await _publish_population_asset(session_factory, "loss-version-mismatch")
    metrics = (("deaths", 12.0, "available", "count"),)
    product = await _create_loss_product(
        seeded_artifact_assessment,
        product_type="casualties",
        version="casualties-v1",
        checksum=_loss_checksum("casualties", metrics),
    )
    await _add_loss_metric(
        seeded_artifact_assessment,
        product,
        metric_key=metrics[0][0],
        numeric_value=metrics[0][1],
        value_status=metrics[0][2],
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.deaths")
    async with session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
            await session.execute(
                update(LossProduct)
                .where(LossProduct.id == product.id)
                .values(algorithm_version="wrong-version")
            )

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    with pytest.raises(MapSourceResolutionError):
        async with session_factory() as session:
            async with session.begin():
                await service.freeze_static_context(
                    session,
                    run.id,
                    seeded_artifact_assessment.catalog,
                )
                await service.build_map_context(session, task.id)


async def test_intensity_product_version_mismatch_fails_closed(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    product = await seeded_artifact_assessment.create_fusion_product(
        version="fusion-v1",
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.intensity")
    async with session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
            await session.execute(
                update(IntensityFieldProduct)
                .where(IntensityFieldProduct.id == product.id)
                .values(algorithm_version="wrong-version")
            )

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
    )
    with pytest.raises(MapSourceResolutionError):
        async with session_factory() as session:
            async with session.begin():
                await service.freeze_static_context(
                    session,
                    run.id,
                    seeded_artifact_assessment.catalog,
                )
                await service.build_map_context(session, task.id)


async def test_product_metric_full_chain_render(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    await _publish_population_asset(session_factory, "product-metric-maps-a")
    metrics = (("deaths", 12.0, "available", "count"),)
    product = await _create_loss_product(
        seeded_artifact_assessment,
        product_type="casualties",
        version="casualties-maps-a",
        checksum=_loss_checksum("casualties", metrics),
    )
    await _add_loss_metric(
        seeded_artifact_assessment,
        product,
        metric_key=metrics[0][0],
        numeric_value=metrics[0][1],
        value_status=metrics[0][2],
    )

    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.deaths")
    async with session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    spec = MapSpecBuilder().build(context)
    result = await renderer.render(spec, tmp_path / "deaths.jpg")
    layers = MapLayerRegistry.build("map.deaths", context)
    metric_layer = next(layer for layer in layers if layer.id == "deaths-town")
    assert metric_layer.source["data"]["features"][0]["properties"]["deaths"] == 12
    assert metric_layer.style["paint"]["fill-color"][2] == ["get", "deaths"]
    assert result.quality.grade == "A"
    assert result.render_manifest["base_provider"] == "gaode"
    binding = await _dependency_binding(
        session_factory,
        task.id,
        "loss.casualties",
    )
    assert result.render_manifest["layer_versions"]["deaths-town"] == binding.bound_version
    assert result.render_manifest["layer_checksums"]["deaths-town"] == binding.bound_checksum


async def test_product_map_metric_encoding_changes_pixels(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
) -> None:
    low_result, context = await _render_product_map(
        fixture=seeded_artifact_assessment,
        session_factory=session_factory,
        production_renderer=production_renderer,
        tmp_path=tmp_path,
        artifact_key="map.deaths",
        product_key="loss.casualties",
        product_type="casualties",
        metrics=(("deaths", 1.0, "available", "count"),),
        publish_towns=True,
    )
    binding = await _dependency_binding(
        session_factory,
        context.production_task_id,
        "loss.casualties",
    )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(LossMetricValue)
                .where(
                    LossMetricValue.product_id == binding.bound_entity_id,
                    LossMetricValue.metric_key == "deaths",
                    LossMetricValue.value_type == "central",
                )
                .values(numeric_value=900)
            )
            product = await session.get(LossProduct, binding.bound_entity_id)
            assert product is not None
            product.output_checksum = _loss_checksum(
                "casualties",
                (("deaths", 900.0, "available", "count"),),
            )
            await session.execute(
                update(ArtifactTaskDependencyBinding)
                .where(
                    ArtifactTaskDependencyBinding.production_task_id
                    == binding.production_task_id,
                    ArtifactTaskDependencyBinding.dependency_key
                    == "loss.casualties",
                )
                .values(bound_checksum=product.output_checksum)
            )

    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    async with session_factory() as session:
        async with session.begin():
            high_context = await service.build_map_context(
                session,
                binding.production_task_id,
            )
    high_result = await renderer.render(
        MapSpecBuilder().build(high_context),
        tmp_path / "deaths-high.jpg",
    )

    assert _central_map_red_mean(high_result.path) > _central_map_red_mean(
        low_result.path
    )


@pytest.mark.parametrize(
    (
        "artifact_key",
        "product_key",
        "product_type",
        "low_metrics",
        "high_metrics",
        "publish_towns",
        "layer_id",
        "metric_property",
        "low_properties",
        "high_properties",
    ),
    (
        (
            "map.economic_loss",
            "loss.economic",
            "economic_loss",
            (("total_loss_yuan", 100_000.0, "available", "yuan"),),
            (("total_loss_yuan", 1_900_000.0, "available", "yuan"),),
            True,
            "economic-loss-town",
            "economic_loss",
            {"economic_loss": 100_000.0},
            {"economic_loss": 1_900_000.0},
        ),
        (
            "map.rescue_demand",
            "loss.resources",
            "resource_demand",
            (("rescue_team.quantity", 1.0, "available", "count"),),
            (("rescue_team.quantity", 100.0, "available", "count"),),
            False,
            "rescue-demand-town",
            "rescue_teams",
            {"rescue_teams": 1.0},
            {"rescue_teams": 100.0},
        ),
        (
            "map.material_demand",
            "loss.resources",
            "resource_demand",
            tuple(
                (f"{kind}.quantity", 1.0, "available", "count")
                for kind in (
                    "tent",
                    "drinking_water",
                    "food",
                    "clothing",
                    "quilt",
                    "blanket",
                    "stretcher",
                    "sickbed",
                    "toilet",
                )
            ),
            tuple(
                (f"{kind}.quantity", 100.0, "available", "count")
                for kind in (
                    "tent",
                    "drinking_water",
                    "food",
                    "clothing",
                    "quilt",
                    "blanket",
                    "stretcher",
                    "sickbed",
                    "toilet",
                )
            ),
            False,
            "material-demand-town",
            "tent",
            {"tent": 1.0, "toilet": 1.0},
            {"tent": 100.0, "toilet": 100.0},
        ),
        (
            "map.building_damage",
            "loss.buildings",
            "building_damage",
            (("severe_or_collapsed_area_m2", 10.0, "available", "m2"),),
            (("severe_or_collapsed_area_m2", 9_500.0, "available", "m2"),),
            True,
            "building-damage-town",
            "damaged_buildings",
            {"damaged_buildings": 10.0},
            {"damaged_buildings": 9_500.0},
        ),
    ),
)
async def test_product_map_matrix_metric_encoding_changes_pixels(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
    artifact_key,
    product_key,
    product_type,
    low_metrics,
    high_metrics,
    publish_towns,
    layer_id,
    metric_property,
    low_properties,
    high_properties,
) -> None:
    low_result, context = await _render_product_map(
        fixture=seeded_artifact_assessment,
        session_factory=session_factory,
        production_renderer=production_renderer,
        tmp_path=tmp_path,
        artifact_key=artifact_key,
        product_key=product_key,
        product_type=product_type,
        metrics=low_metrics,
        publish_towns=publish_towns,
    )
    low_layer = next(
        layer
        for layer in MapLayerRegistry.build(artifact_key, context)
        if layer.id == layer_id
    )
    low_feature = low_layer.source["data"]["features"][0]
    for property_name, expected_value in low_properties.items():
        assert low_feature["properties"][property_name] == expected_value
    assert low_layer.style["paint"]["fill-color"][2] == ["get", metric_property]
    assert low_result.non_empty_ratio > 0.2

    low_binding = await _dependency_binding(
        session_factory,
        context.production_task_id,
        product_key,
    )
    assert low_result.render_manifest["layer_checksums"][layer_id] == (
        low_binding.bound_checksum
    )
    assert low_result.render_manifest["layer_versions"][layer_id] == (
        low_binding.bound_version
    )

    high_result, high_context = await _replace_product_metrics_and_render(
        session_factory=session_factory,
        production_renderer=production_renderer,
        tmp_path=tmp_path,
        context=context,
        artifact_key=artifact_key,
        product_key=product_key,
        product_type=product_type,
        metrics=high_metrics,
    )
    high_layer = next(
        layer
        for layer in MapLayerRegistry.build(artifact_key, high_context)
        if layer.id == layer_id
    )
    high_feature = high_layer.source["data"]["features"][0]
    for property_name, expected_value in high_properties.items():
        assert high_feature["properties"][property_name] == expected_value
    assert high_layer.style["paint"]["fill-color"][2] == ["get", metric_property]
    high_binding = await _dependency_binding(
        session_factory,
        high_context.production_task_id,
        product_key,
    )
    assert high_result.render_manifest["layer_checksums"][layer_id] == (
        high_binding.bound_checksum
    )
    assert high_result.render_manifest["layer_versions"][layer_id] == (
        high_binding.bound_version
    )
    assert _central_map_red_mean(high_result.path) > _central_map_red_mean(
        low_result.path
    )


@pytest.mark.parametrize(
    "artifact_key,product_key",
    (
        ("map.intensity", "intensity.fusion"),
        ("map.economic_loss", "loss.economic"),
        ("map.rescue_demand", "loss.resources"),
        ("map.deaths", "loss.casualties"),
        ("map.injuries", "loss.casualties"),
        ("map.buried", "loss.casualties"),
        ("map.material_demand", "loss.resources"),
        ("map.building_damage", "loss.buildings"),
    ),
)
async def test_a_class_product_dependencies_are_persisted(
    artifact_key,
    product_key,
    seeded_artifact_assessment,
) -> None:
    task = await _prepare_product_dependencies(
        seeded_artifact_assessment,
        artifact_key,
    )
    bindings = await seeded_artifact_assessment.bindings_for(task.id)

    binding = next(
        item
        for item in bindings
        if item.dependency_kind == "assessment_product"
        and item.dependency_key == product_key
    )
    assert binding.resolution_status == "bound"
    assert binding.bound_entity_id is not None
    assert binding.bound_checksum is not None
