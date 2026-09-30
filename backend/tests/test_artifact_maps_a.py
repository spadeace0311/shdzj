from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

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
from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings
from app.db import engine
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import DataAssetVersion
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.raster_repository import save_raster_version
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import DataAssetService
from app.loss.models import LossMetricValue, LossProduct
from tests.basemap_fixtures import make_in_memory_package, png_tile_bytes
from tests.data_asset_helpers import publish_new_population_version


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


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


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
) -> None:
    async with fixture._session_factory() as session:
        async with session.begin():
            session.add(
                LossMetricValue(
                    product_id=product.id,
                    area_scope="town",
                    area_code="310115000001",
                    area_name="fixture town",
                    metric_key=metric_key,
                    value_type="central",
                    value_status=value_status,
                    numeric_value=numeric_value,
                    unit="count",
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


async def _publish_asset(
    session_factory,
    asset_key: str,
    *,
    records: tuple[NormalizedRecord, ...] = (),
    spatial_extent: tuple[float, float, float, float] | None = None,
) -> None:
    definition = get_asset_definition(asset_key)
    async with session_factory() as session:
        async with session.begin():
            from app.data_assets.models import DataAsset

            existing = await session.scalar(
                select(DataAssetVersion)
                .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
                .where(
                    DataAsset.asset_key == asset_key,
                    DataAsset.region_id == definition.region_id,
                    DataAssetVersion.status == "published",
                )
            )
            if existing is not None:
                return
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
            version = await session.get(DataAssetVersion, job.asset_version_id)
            if version is None:
                raise LookupError("maps-a candidate asset version not found")
            version.status = "validated"
            version.validated_at = datetime.now(UTC)
            await session.flush()
            version.status = "published"
            version.published_at = datetime.now(UTC)


def _admin_city_record() -> NormalizedRecord:
    return NormalizedRecord(
        row_number=1,
        business_key="shanghai",
        properties={"name": "Shanghai"},
        geometry_wkt=(
            "MULTIPOLYGON (((121.2 30.9, 121.8 30.9, "
            "121.8 31.5, 121.2 31.5, 121.2 30.9)))"
        ),
    )


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


async def test_production_population_context_resolves_published_vector(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    await publish_new_population_version(session_factory, f"maps-a-{id(seeded_artifact_assessment)}")
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
    seeded_imported_version,
    session_factory,
) -> None:
    descriptor = GeoTiffAssetImporter().load(
        seeded_imported_version.source_path,
        seeded_imported_version.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            await save_raster_version(
                session,
                seeded_imported_version.version_id,
                seeded_imported_version.source_path,
                descriptor,
            )
            version = await session.get(
                DataAssetVersion,
                seeded_imported_version.version_id,
            )
            assert version is not None
            version.status = "validated"
            await session.flush()
            version.status = "published"
            version.published_at = datetime.now(UTC)

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
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
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.city",
        records=(_admin_city_record(),),
        spatial_extent=(121.2, 30.9, 121.8, 31.5),
    )
    await _publish_asset(session_factory, "shanghai.fault")
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(_settings_manifests(), provider="gaode"),
            "tianditu": make_in_memory_package(_settings_manifests(), provider="tianditu"),
        },
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
    assert "检索范围内无活动断裂记录" in MapSpecBuilder().build(context).source_notes


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
    await publish_new_population_version(session_factory, "product-metric-maps-a")
    product = await _create_loss_product(
        seeded_artifact_assessment,
        product_type="casualties",
        version="casualties-maps-a",
        checksum="e" * 64,
    )
    await _add_loss_metric(
        seeded_artifact_assessment,
        product,
        metric_key="deaths",
        numeric_value=12,
        value_status="available",
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
    assert result.quality.grade == "A"
    assert result.render_manifest["base_provider"] == "gaode"


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
