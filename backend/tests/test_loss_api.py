from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

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
from app.db import engine
from app.main import app
from tests.data_asset_helpers import _ensure_published_admin_town


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
        0,
        1000,
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
        metrics=(_metric(f"{product_type.value}.total"),),
        raster=None,
        reason=None,
    )


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


async def _request_loss(run_id: UUID):
    return await _request(f"/api/v1/assessments/runs/{run_id}/loss")


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
        f"{product_id}/buildings_collapsed_area_m2/0/0/0.png"
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


async def test_reader_role_cannot_import_or_publish_data_assets() -> None:
    response = await _request_data_asset_publish_as_viewer()
    assert response.status_code == 403
