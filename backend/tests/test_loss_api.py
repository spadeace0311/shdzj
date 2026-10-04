from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

from app.assessment.models import AssessmentRun, AssessmentTask
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition
from app.loss.domain import (
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
)
from app.loss.models import LossMetricValue, LossProduct, LossProductRaster
from app.loss.repository import (
    LossMetricValueWrite,
    LossProductWrite,
    LossRasterBandWrite,
    LossRasterWrite,
    LossRepository,
)
from app.data_assets.models import DataAssetRecord
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.db import engine
from app.main import app
from tests.data_asset_helpers import _ensure_published_admin_town
from rasterio.io import MemoryFile


_LOSS_RASTER_NAMESPACE = "loss-raster-content-v1"


@pytest.fixture(autouse=True)
async def clean_loss_api_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(LossProductRaster))
            await session.execute(delete(LossMetricValue))
            await session.execute(delete(LossProduct))
    yield
    await engine.dispose()


@dataclass(frozen=True, slots=True)
class SeededLossApiProducts:
    run_id: UUID
    building_product_id: UUID


async def _seed_loss_products(
    session_factory,
    run_id: UUID,
) -> SeededLossApiProducts:
    await _ensure_published_admin_town(session_factory)
    async with session_factory() as session:
        async with session.begin():
            task_ids = dict(
                (
                    await session.execute(
                        select(
                            AssessmentTask.task_key,
                            AssessmentTask.id,
                        ).where(AssessmentTask.run_id == run_id)
                    )
                ).all()
            )
            repository = LossRepository()
            building = await repository.write_product(
                session,
                _building_write(run_id, task_ids["loss.buildings"]),
            )
            for product_type, task_key in (
                (LossProductType.POPULATION_IMPACT, "loss.population"),
                (LossProductType.CASUALTIES, "loss.casualties"),
                (LossProductType.ECONOMIC_LOSS, "loss.economic"),
                (LossProductType.RESOURCE_DEMAND, "loss.resources"),
                (LossProductType.VALIDATION, "loss.validate"),
            ):
                await repository.write_product(
                    session,
                    _metric_write(run_id, task_ids[task_key], product_type),
                )
    return SeededLossApiProducts(
        run_id=run_id,
        building_product_id=building.id,
    )


def _metric(
    metric_key: str = "building.damaged_area",
    numeric_value: float | None = 10.0,
    *,
    area_scope: str = "city",
    area_code: str = "310000",
    area_name: str | None = "上海市",
) -> LossMetricValueWrite:
    status = (
        LossMetricValueStatus.AVAILABLE
        if numeric_value is not None
        else LossMetricValueStatus.UNAVAILABLE
    )
    return LossMetricValueWrite(
        area_scope=area_scope,
        area_code=area_code,
        area_name=area_name,
        metric_key=metric_key,
        value_type=LossValueType.CENTRAL,
        value_status=status,
        numeric_value=numeric_value,
        unit="m2",
        precision=2,
        quality_grade=LossQualityGrade.L2,
        note=None,
    )


def _building_write(run_id: UUID, task_id: UUID) -> LossProductWrite:
    definition = GridDefinition(
        "loss-api-grid",
        "EPSG:32651",
        1000,
        356000,
        3450000,
        1,
        1,
    )
    values = np.asarray([[1.0]], dtype=np.float64)
    band_name = "buildings_collapsed_area_m2"
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
        checksum_namespace=_LOSS_RASTER_NAMESPACE,
    )
    raster = LossRasterWrite(
        raster_version="loss-api-grid-v1",
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
    )
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
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
        output_checksum="e" * 64,
        statistics={"town_count": 1},
        metrics=(
            _metric(),
            _metric(
                area_scope="town",
                area_code="310115000001",
                area_name="town-0",
            ),
        ),
        raster=raster,
        reason=None,
    )


def _metric_write(
    run_id: UUID,
    task_id: UUID,
    product_type: LossProductType,
    numeric_value: float | None = 10.0,
) -> LossProductWrite:
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=product_type,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L2,
        calibration_status=LossCalibrationStatus.REFERENCE_UNCALIBRATED,
        coverage_ratio=1.0,
        partial_scope=product_type is LossProductType.ECONOMIC_LOSS,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version=f"{product_type.value}-v1",
        parameter_version="shanghai-loss-reference-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum=f"{product_type.value:0<64}"[:64],
        statistics={},
        metrics=(_metric(f"{product_type.value}.total", numeric_value),),
        raster=None,
        reason=None,
    )


async def _create_run_id(session_factory) -> UUID:
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.assessment.repository import AssessmentRepository
    from app.events.domain import EventKind, NormalizedEvent
    from app.events.models import EventLifecycleOutbox
    from app.events.response_rules import ResponseInput
    from app.events.service import EventService
    from app.regions.domain import RegionContext
    from tests.data_asset_helpers import _with_boundary

    boundary_version = await _with_boundary(session_factory)
    origin_time = datetime(2026, 9, 26, 1, 0, tzinfo=UTC) + timedelta(
        seconds=int(uuid4().int % 3600) + 180
    )
    received_at = origin_time + timedelta(minutes=3)
    source_event_id = f"LOSS-API-{uuid4()}"
    source = f"loss-{uuid4().hex[:16]}"
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
            source=source,
        source_event_id=source_event_id,
        origin_time=origin_time,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="loss api test",
        report_time=datetime(2026, 9, 26, 1, 2, tzinfo=UTC),
    )
    raw_payload = {"EventID": event.source_event_id, "type": "reviewed"}
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload=raw_payload,
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version=boundary_version,
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id,
                    EventLifecycleOutbox.trigger_type == "assessment.requested",
                )
            )
            assert outbox is not None
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return run.id


async def _insert_standalone_run(session_factory) -> UUID:
    from datetime import UTC, datetime, timedelta

    raw_message_id = uuid4()
    event_id = uuid4()
    revision_id = uuid4()
    outbox_id = uuid4()
    run_id = uuid4()
    origin_time = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)
    deadline_at = origin_time + timedelta(minutes=5)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO raw_messages (
                        id, source, message_kind, checksum, payload
                    )
                    VALUES (
                        CAST(:id AS uuid), :source, :message_kind,
                        :checksum, '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": raw_message_id,
                    "source": "loss-api-test",
                    "message_kind": "formal",
                    "checksum": f"raw-{raw_message_id}",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO earthquake_events (
                        id, source, canonical_source_id, event_type,
                        origin_time, longitude, latitude, depth_km,
                        magnitude, place, geom
                    )
                    VALUES (
                        CAST(:id AS uuid), :source, :canonical_source_id,
                        :event_type, :origin_time, 121.5, 31.2, 10, 5.2,
                        :place, ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326)
                    )
                    """
                ),
                {
                    "id": event_id,
                    "source": "loss-api-test",
                    "canonical_source_id": f"loss-api-test:{event_id}",
                    "event_type": "formal",
                    "origin_time": origin_time,
                    "place": "loss api test",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO earthquake_revisions (
                        id, event_id, raw_message_id, revision_no,
                        revision_kind, origin_time, longitude, latitude,
                        depth_km, magnitude, place
                    )
                    VALUES (
                        CAST(:id AS uuid), CAST(:event_id AS uuid),
                        CAST(:raw_message_id AS uuid), :revision_no,
                        :revision_kind, :origin_time, 121.5, 31.2, 10,
                        5.2, :place
                    )
                    """
                ),
                {
                    "id": revision_id,
                    "event_id": event_id,
                    "raw_message_id": raw_message_id,
                    "revision_no": 1,
                    "revision_kind": "formal",
                    "origin_time": origin_time,
                    "place": "loss api test",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO event_lifecycle_outbox (
                        id, event_id, revision_id, trigger_type,
                        trigger_reason, payload, status
                    )
                    VALUES (
                        CAST(:id AS uuid), CAST(:event_id AS uuid),
                        CAST(:revision_id AS uuid), :trigger_type,
                        :trigger_reason, '{}'::jsonb, :status
                    )
                    """
                ),
                {
                    "id": outbox_id,
                    "event_id": event_id,
                    "revision_id": revision_id,
                    "trigger_type": "assessment.requested",
                    "trigger_reason": "loss-api-test",
                    "status": "pending",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO assessment_runs (
                        id, event_id, revision_id, outbox_id, run_no,
                        trigger_reason, t1_at, deadline_at, snapshot,
                        report_ingested_at, deadline_basis_at
                    )
                    VALUES (
                        CAST(:id AS uuid), CAST(:event_id AS uuid),
                        CAST(:revision_id AS uuid), CAST(:outbox_id AS uuid),
                        :run_no, :trigger_reason, :t1_at, :deadline_at,
                        '{}'::jsonb, :report_ingested_at, :deadline_basis_at
                    )
                    """
                ),
                {
                    "id": run_id,
                    "event_id": event_id,
                    "revision_id": revision_id,
                    "outbox_id": outbox_id,
                    "run_no": 1,
                    "trigger_reason": "loss-api-test",
                    "t1_at": origin_time,
                    "deadline_at": deadline_at,
                    "report_ingested_at": origin_time,
                    "deadline_basis_at": origin_time,
                },
            )
            task_keys = (
                ("intensity.model", "intensity", "model_intensity", 1),
                ("intensity.instrument", "intensity", "instrument_intensity", 2),
                ("intensity.fusion", "intensity", "fusion_intensity", 3),
                ("loss.population", "loss", "population_impact", 4),
                ("loss.casualties", "loss", "casualties", 5),
                ("loss.buildings", "loss", "building_damage", 6),
                ("loss.economic", "loss", "economic_loss", 7),
                ("loss.resources", "loss", "resource_demand", 8),
                ("loss.validate", "loss", "loss_validation", 9),
                (
                    "report.rapid_assessment",
                    "report",
                    "rapid_assessment_report",
                    10,
                ),
                (
                    "workgroup.response_tasks",
                    "coordination",
                    "workgroup_tasks",
                    11,
                ),
            )
            for task_key, task_type, component, sequence in task_keys:
                await session.execute(
                    text(
                        """
                        INSERT INTO assessment_tasks (
                            id, run_id, task_key, task_type, component,
                            sequence, deadline_at
                        )
                        VALUES (
                            CAST(:id AS uuid), CAST(:run_id AS uuid),
                            :task_key, :task_type, :component, :sequence,
                            :deadline_at
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "run_id": run_id,
                        "task_key": task_key,
                        "task_type": task_type,
                        "component": component,
                        "sequence": sequence,
                        "deadline_at": deadline_at,
                    },
                )
    return run_id


async def _seed_run_and_products(session_factory) -> SeededLossApiProducts:
    await _ensure_published_admin_town(session_factory)
    run_id = await _create_run_id(session_factory)
    return await _seed_loss_products(session_factory, run_id)


async def _locked_admin_version(session_factory, run_id: UUID):
    async with session_factory() as session:
        return await DataAssetSnapshotService().get_locked_version(
            session,
            run_id=run_id,
            asset_key="shanghai.admin.town",
        )


async def _record_geometry(session_factory, version_id: UUID, area_code: str) -> dict:
    import json

    async with session_factory() as session:
        geometry = await session.scalar(
            select(func.ST_AsGeoJSON(DataAssetRecord.geom)).where(
                DataAssetRecord.version_id == version_id,
                DataAssetRecord.business_key == area_code,
            )
        )
    return json.loads(str(geometry))


async def _republish_changed_admin_town(session_factory) -> UUID:
    from uuid import uuid4 as local_uuid4

    from app.data_assets.domain import NormalizedRecord, NormalizedTableData
    from app.data_assets.service import DataAssetService
    from tests.data_asset_helpers import (
        FIXTURE_ACTOR,
        _definition,
        _populate,
        _queue_candidate,
        _town_codes,
    )

    definition = _definition("shanghai.admin.town")
    records = tuple(
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={"ID": town_code, "NAME": f"changed-town-{index}"},
            geometry_wkt=(
                "MULTIPOLYGON (((121.7 31.2, 121.8 31.2, 121.8 31.3, "
                "121.7 31.3, 121.7 31.2)))"
            ),
        )
        for index, town_code in enumerate(_town_codes(), start=1)
    )
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key=definition.asset_key,
                version=f"2022.2-{local_uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                NormalizedTableData(
                    columns=("ID", "NAME"),
                    records=records,
                    source_crs="EPSG:4326",
                    spatial_extent=(121.7, 31.2, 121.8, 31.3),
                ),
                importer="geojson",
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=FIXTURE_ACTOR,
            )
            if not report.publishable:
                raise ValueError("changed admin town version is not publishable")
            published = await service.publish_version(
                session,
                job.asset_version_id,
                FIXTURE_ACTOR,
                "changed boundary fixture",
            )
            return published.id


async def _request(
    path: str,
    *,
    role: str = "group_member",
    method: str = "GET",
):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role=role,
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.request(method, path)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def _request_loss(run_id: UUID, *, role: str = "group_member"):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss",
        role=role,
    )


async def _request_loss_areas(run_id: UUID, scope: str):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss/areas?scope={scope}"
    )


async def _request_loss_artifact(run_id: UUID, product_id: UUID):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss/artifact"
        f"?product_id={product_id}&band=buildings_collapsed_area_m2"
    )


async def _request_loss_tile(run_id: UUID, product_id: UUID):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss/artifact/"
        f"{product_id}/buildings_collapsed_area_m2/10/857/418.png"
    )


async def _request_data_asset_publish_as_viewer():
    return await _request(
        f"/api/v1/data-asset-versions/{uuid4()}/publish",
        role="viewer",
        method="POST",
    )


async def test_get_loss_summary_returns_versions_and_quality(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss(seeded.run_id)
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(seeded.run_id)
    assert body["products"][0]["status"] == "complete"
    assert body["products"][0]["quality_grade"] == "L2"
    assert body["products"][0]["calibration_status"] == "reference_uncalibrated"
    assert body["products"][0]["metrics"][0]["value_type"] == "central"
    assert body["products"][0]["metrics"][0]["value_status"] == "available"


async def test_get_loss_areas_rejects_invalid_scope(session_factory) -> None:
    response = await _request_loss_areas(uuid4(), "not-a-scope")
    assert response.status_code == 422


async def test_get_loss_artifact_returns_metadata_not_cell_json(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss_artifact(
        seeded.run_id,
        seeded.building_product_id,
    )
    assert response.status_code == 200
    assert response.json()["product_id"] == str(seeded.building_product_id)
    assert response.json()["bands"][0]["name"] == "buildings_collapsed_area_m2"
    assert "cells" not in response.json()
    assert response.json()["tile_template"].endswith("/{z}/{x}/{y}.png")


async def test_get_town_loss_areas_returns_geometry_and_metrics(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss_areas(seeded.run_id, "town")
    assert response.status_code == 200
    feature = response.json()["features"][0]
    assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert feature["metrics"]


async def test_current_assessment_loss_is_none_without_products(
    session_factory,
    seeded_assessment_run,
) -> None:
    async with session_factory() as session:
        run = await session.get(AssessmentRun, seeded_assessment_run)
        assert run is not None
        event_id = run.event_id
    response = await _request(
        f"/api/v1/assessments/events/{event_id}/current"
    )
    assert response.status_code == 200
    assert response.json()["loss"] is None


@pytest.mark.filterwarnings("ignore:Use `@` matmul")
@pytest.mark.filterwarnings("ignore:Dataset has no geotransform")
async def test_get_loss_tile_returns_png_from_persisted_raster(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss_tile(
        seeded.run_id,
        seeded.building_product_id,
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    with MemoryFile(response.content) as memory:
        with memory.open() as dataset:
            pixels = dataset.read()
    assert pixels.size > 0
    assert np.any(pixels != 0)


async def test_loss_areas_use_run_locked_boundary(
    session_factory,
) -> None:
    seeded = await _seed_run_and_products(session_factory)
    locked_version = await _locked_admin_version(
        session_factory,
        seeded.run_id,
    )
    assert locked_version is not None
    old_geometry = await _record_geometry(
        session_factory,
        locked_version.id,
        "310115000001",
    )

    new_version_id = await _republish_changed_admin_town(session_factory)
    assert new_version_id != locked_version.id

    response = await _request_loss_areas(seeded.run_id, "town")
    assert response.status_code == 200
    feature = response.json()["features"][0]
    assert feature["area_code"] == "310115000001"
    assert feature["geometry"] == old_geometry


async def test_artifact_rejects_product_from_other_run(
    session_factory,
) -> None:
    await _ensure_published_admin_town(session_factory)
    first_run_id = await _insert_standalone_run(session_factory)
    second_run_id = await _insert_standalone_run(session_factory)
    assert first_run_id != second_run_id
    first = await _seed_loss_products(session_factory, first_run_id)
    second = await _seed_loss_products(session_factory, second_run_id)

    response = await _request_loss_artifact(
        first.run_id,
        second.building_product_id,
    )

    assert response.status_code == 404


async def test_null_metric_serializes_as_json_null(
    session_factory,
    seeded_assessment_run,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            task_id = await session.scalar(
                select(AssessmentTask.id).where(
                    AssessmentTask.run_id == seeded_assessment_run,
                    AssessmentTask.task_key == "loss.population",
                )
            )
            assert task_id is not None
            write = _metric_write(
                seeded_assessment_run,
                task_id,
                LossProductType.POPULATION_IMPACT,
                numeric_value=None,
            )
            await LossRepository().write_product(session, write)

    response = await _request(
        f"/api/v1/assessments/runs/{seeded_assessment_run}/loss/"
        "products/population_impact"
    )
    assert response.status_code == 200
    metric = response.json()["metrics"][0]
    assert metric["numeric_value"] is None
    assert metric["value_status"] == "unavailable"


async def test_unauthorized_loss_read_is_rejected(
    session_factory,
    seeded_assessment_run,
) -> None:
    response = await _request_loss(
        seeded_assessment_run,
        role="not_allowed",
    )
    assert response.status_code == 403


async def test_artifact_unknown_product_and_band_return_404(
    session_factory,
    seeded_assessment_run,
) -> None:
    missing_product = await _request_loss_artifact(
        seeded_assessment_run,
        uuid4(),
    )
    assert missing_product.status_code == 404

    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    unknown_band = await _request(
        f"/api/v1/assessments/runs/{seeded.run_id}/loss/artifact"
        f"?product_id={seeded.building_product_id}&band=missing_band"
    )
    assert unknown_band.status_code == 404


async def test_reader_role_cannot_import_or_publish_data_assets() -> None:
    response = await _request_data_asset_publish_as_viewer()
    assert response.status_code == 403
