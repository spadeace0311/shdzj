from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.artifacts.context import ProductionContextService
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.renderers.map_layers import (
    B_CLASS_ARTIFACTS,
    MapDegradePolicy,
    MapLayerRegistry,
    MapQualityPolicy,
    RequiredDependencyMissingError,
)
from app.artifacts.renderers.map_renderer import (
    BrowserPool,
    MapRenderer,
    MapSpecBuilder,
)
from app.artifacts.renderers.map_sources import MapSourceResolutionError
from app.assessment.models import AssessmentTask
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import DataAsset, DataAssetRecord, DataAssetVersion
from app.data_assets.repository import DataAssetRepository
from app.data_assets.service import DataAssetService, compute_table_checksum
from app.db import engine
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition
from app.loss.domain import (
    LossCalibrationStatus,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
)
from app.loss.repository import (
    LossProductWrite,
    LossRasterBandWrite,
    LossRasterWrite,
    LossRepository,
)
from tests.basemap_fixtures import make_in_memory_package, png_tile_bytes
from tests.data_asset_helpers import _town_records
from tests.test_artifact_maps_a import (
    _admin_city_record,
    _building_town_records,
    _settings_manifests,
)


_CREATED_VERSION_IDS: set[UUID] = set()
_CREATED_ASSET_IDS: set[UUID] = set()


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_degraded_assets(session_factory):
    yield
    version_ids = list(_CREATED_VERSION_IDS)
    asset_ids = list(_CREATED_ASSET_IDS)
    _CREATED_VERSION_IDS.clear()
    _CREATED_ASSET_IDS.clear()
    if not version_ids and not asset_ids:
        return
    async with session_factory() as session:
        async with session.begin():
            if version_ids:
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.id.in_(version_ids)
                    )
                )
            if asset_ids:
                await session.execute(
                    delete(DataAsset).where(DataAsset.id.in_(asset_ids))
                )


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
                    version=f"{asset_key}-degraded-{datetime.now(UTC).timestamp()}",
                    source_uri="https://example.gov.invalid/degraded",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="degraded production fixture",
                    file_name=asset_key,
                    file_format="geojson",
                    file_size_bytes=1,
                    checksum="a" * 64,
                    relative_path=f"fixture/{asset_key}",
                    requested_by="degraded-fixture",
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
                actor="degraded-fixture",
            )
            if not report.publishable:
                raise AssertionError(
                    f"{asset_key} degraded fixture failed validation: "
                    f"{[issue.message for issue in report.errors]}"
                )
            version = await service.publish_version(
                session,
                job.asset_version_id,
                "degraded-fixture",
                "degraded production fixture",
            )
            _CREATED_VERSION_IDS.add(version.id)
            return version.id


async def _insert_published_vector_asset(
    session_factory,
    asset_key: str,
    *,
    records: tuple[NormalizedRecord, ...],
) -> UUID:
    normalized = NormalizedTableData(
        columns=tuple(
            sorted({key for record in records for key in record.properties})
        ),
        records=records,
        source_crs="EPSG:4326",
        spatial_extent=None,
    )
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == asset_key,
                    DataAsset.region_id == "shanghai",
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=asset_key,
                    region_id="shanghai",
                    name=asset_key,
                    data_type="vector",
                    spatial_granularity="feature",
                    responsibility_unit="degraded-fixture",
                    update_interval_days=365,
                    is_core=False,
                    contract={},
                )
                session.add(asset)
                await session.flush()
                _CREATED_ASSET_IDS.add(asset.id)
            version = DataAssetVersion(
                asset_id=asset.id,
                version=f"{asset_key}-empty-{uuid4()}",
                status="imported",
                source_uri="https://example.gov.invalid/empty",
                license_name=None,
                acquired_at=None,
                valid_from=None,
                valid_to=None,
                quality_grade=None,
                change_note="empty query fixture",
                schema_summary={
                    "columns": list(normalized.columns),
                    "normalized_checksum": None,
                },
                record_count=normalized.record_count,
                spatial_extent=None,
                source_crs="EPSG:4326",
                checksum=None,
                managed_path=f"fixture/{asset_key}",
                imported_by="degraded-fixture",
                reviewed_by=None,
                imported_at=now,
                validated_at=None,
                published_at=None,
                retired_at=None,
            )
            session.add(version)
            await session.flush()
            _CREATED_VERSION_IDS.add(version.id)
            for record in records:
                session.add(
                    DataAssetRecord(
                        version_id=version.id,
                        row_number=record.row_number,
                        business_key=record.business_key,
                        properties=dict(record.properties),
                        geom=(
                            WKTElement(record.geometry_wkt, srid=4326)
                            if record.geometry_wkt is not None
                            else None
                        ),
                    )
                )
            await session.flush()
            persisted_records = await DataAssetRepository().list_records(
                session,
                version.id,
            )
            persisted_normalized = NormalizedTableData(
                columns=normalized.columns,
                records=tuple(
                    NormalizedRecord(
                        row_number=record.row_number,
                        business_key=record.business_key,
                        properties=dict(record.properties),
                        geometry_wkt=record.geometry_wkt,
                    )
                    for record in persisted_records
                ),
                source_crs="EPSG:4326",
                spatial_extent=None,
            )
            checksum = compute_table_checksum(persisted_normalized)
            version.schema_summary = {
                "columns": list(normalized.columns),
                "normalized_checksum": checksum,
            }
            version.checksum = checksum
            version.record_count = persisted_normalized.record_count
            await session.flush()
            version.status = "validated"
            version.validated_at = now
            await session.flush()
            version.status = "published"
            version.published_at = now
            await session.flush()
            published = await DataAssetRepository().get_published_version(
                session,
                asset_key=asset_key,
                region_id="shanghai",
            )
            assert published is not None
            return version.id


async def _build_context(
    seeded_artifact_assessment,
    session_factory,
    service,
    artifact_key: str,
) -> object:
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, artifact_key)
    async with session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            return await service.build_map_context(session, task.id)


async def _write_building_grid_product(
    seeded_artifact_assessment,
    session_factory,
) -> str:
    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id
                    == seeded_artifact_assessment.assessment_run_id,
                    AssessmentTask.task_key == "loss.buildings",
                )
            )
            if task is None:
                from app.assessment.models import AssessmentRun

                run = await session.get(
                    AssessmentRun,
                    seeded_artifact_assessment.assessment_run_id,
                )
                task = AssessmentTask(
                    run_id=run.id,
                    task_key="loss.buildings",
                    task_type="loss",
                    component="degraded-fixture",
                    sequence=1,
                    deadline_at=run.deadline_at,
                    status="succeeded",
                )
                session.add(task)
                await session.flush()

            definition = GridDefinition(
                "building-grid-v1",
                "EPSG:32651",
                1000,
                356000,
                3450000,
                2,
                2,
            )
            values = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
            band_name = "buildings_collapsed_area_m2_central"
            manifest = {
                "grid": {
                    "version": definition.version,
                    "crs": definition.crs,
                    "resolution_m": definition.resolution_m,
                    "origin_x": definition.origin_x,
                    "origin_y": definition.origin_y,
                    "width": definition.width,
                    "height": definition.height,
                },
                "bands": [
                    {
                        "number": 1,
                        "name": band_name,
                        "unit": "m2",
                        "precision": 2,
                    }
                ],
            }
            checksum = RasterCodec.content_checksum(
                definition,
                [(band_name, values)],
                manifest,
                checksum_namespace="loss-raster-content-v1",
            )
            write = LossProductWrite(
                run_id=seeded_artifact_assessment.assessment_run_id,
                task_id=task.id,
                product_type=LossProductType.BUILDING_DAMAGE,
                status=LossProductStatus.COMPLETE,
                quality_grade=LossQualityGrade.L2,
                calibration_status=LossCalibrationStatus.REFERENCE_UNCALIBRATED,
                coverage_ratio=1.0,
                partial_scope=False,
                needs_review=False,
                spatialized_estimate=True,
                algorithm_version="building-structure-matrix-v1",
                parameter_version="shanghai-loss-reference-v1",
                region_profile_version="shanghai-loss-region-v1",
                input_fingerprint="c" * 64,
                input_checksum="d" * 64,
                output_checksum="f" * 64,
                statistics={},
                metrics=(),
                raster=LossRasterWrite(
                    raster_version="building-grid-v1",
                    definition=definition,
                    bands=(
                        LossRasterBandWrite(
                            name=band_name,
                            values=values,
                            unit="m2",
                            precision=2,
                        ),
                    ),
                    band_manifest=manifest,
                    checksum=checksum,
                    spatial_allocation_rule="town-uniform-v1",
                    coverage_ratio=1.0,
                ),
                reason=None,
            )
            product = await LossRepository().write_product(session, write)
            return product.input_checksum


@pytest.mark.parametrize("artifact_key", B_CLASS_ARTIFACTS)
async def test_b_class_missing_optional_data_creates_degraded_file(
    artifact_key,
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
    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    context = await _build_context(
        seeded_artifact_assessment,
        session_factory,
        service,
        artifact_key,
    )

    layers = MapLayerRegistry.build(artifact_key, context)
    quality = MapQualityPolicy.evaluate(artifact_key, layers)
    result = await renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / f"{artifact_key}.jpg",
    )

    optional_layer = next(
        layer for layer in layers if bool(layer.metadata.get("optional"))
    )
    assert optional_layer.metadata["source_status"] == "missing"
    assert result.task_status == "degraded"
    assert result.quality.needs_review is True
    assert result.quality.grade == "B"
    assert optional_layer.metadata["source_key"] in result.quality.missing_assets
    assert "待复核" in result.render_manifest["quality"]["degradation_reasons"][0]
    assert quality.degradation_reasons == result.quality.degradation_reasons


async def test_b_class_missing_admin_boundary_fails_hard(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
) -> None:
    package, _ = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    with pytest.raises(MapSourceResolutionError):
        await _build_context(
            seeded_artifact_assessment,
            session_factory,
            service,
            "map.shelter_emergency",
        )


async def test_b_class_empty_optional_query_is_not_review(
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
    await _insert_published_vector_asset(
        session_factory,
        "shanghai.shelter.emergency",
        records=(
            NormalizedRecord(
                row_number=1,
                business_key="shelter-1",
                properties={"id": "shelter-1", "name": "far shelter"},
                geometry_wkt="POINT (126.0 36.0)",
            ),
        ),
    )

    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    context = await _build_context(
        seeded_artifact_assessment,
        session_factory,
        service,
        "map.shelter_emergency",
    )
    assert (
        context.asset_versions["shanghai.shelter.emergency"].resolution_status
        == "bound"
    )
    layers = MapLayerRegistry.build("map.shelter_emergency", context)
    optional_layer = next(
        layer for layer in layers if bool(layer.metadata.get("optional"))
    )
    assert optional_layer.metadata["source_status"] == "verified_empty"

    spec = MapSpecBuilder().build(context)
    result = await renderer.render(spec, tmp_path / "empty.jpg")
    assert "检索范围内无记录" in spec.source_notes
    assert result.quality.needs_review is False
    assert result.task_status == "succeeded"


def test_b_class_policy_blocks_without_event_or_boundary() -> None:
    decision = MapDegradePolicy.evaluate(
        "map.shelter_emergency",
        (),
        {},
    )
    assert decision.status == "blocked"
    assert set(decision.missing_assets) == {"event", "shanghai.admin.city"}


async def test_c_class_grid_requires_traceable_spatialized_input(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
    tmp_path: Path,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.town",
        records=_town_records().records,
        spatial_extent=(121.4, 31.1, 121.6, 31.4),
    )
    await _publish_asset(
        session_factory,
        "shanghai.building.town",
        records=_building_town_records(),
    )
    input_checksum = await _write_building_grid_product(
        seeded_artifact_assessment,
        session_factory,
    )

    package, renderer = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    context = await _build_context(
        seeded_artifact_assessment,
        session_factory,
        service,
        "map.building_grid",
    )
    result = await renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / "grid.jpg",
    )

    assert result.task_status == "degraded"
    assert result.quality.spatialized_estimate is True
    assert result.quality.grade == "C"
    assert result.render_manifest["allocation_rule"] == "town-uniform-v1"
    assert result.render_manifest["allocation_inputs"]["loss.buildings"] == input_checksum
    assert result.render_manifest["allocation_inputs"]["shanghai.building.town"]
    assert result.render_manifest["allocation_inputs"]["shanghai.admin.town"]


async def test_c_class_grid_missing_model_fails_closed(
    seeded_artifact_assessment,
    session_factory,
    production_renderer,
) -> None:
    await _publish_asset(
        session_factory,
        "shanghai.admin.town",
        records=_town_records().records,
        spatial_extent=(121.4, 31.1, 121.6, 31.4),
    )
    await _publish_asset(
        session_factory,
        "shanghai.building.town",
        records=_building_town_records(),
    )

    package, _ = production_renderer
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={"gaode": package},
    )
    with pytest.raises(RequiredDependencyMissingError):
        await _build_context(
            seeded_artifact_assessment,
            session_factory,
            service,
            "map.building_grid",
        )
