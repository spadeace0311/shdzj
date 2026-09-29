import numpy as np
import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentTask
from app.db import engine
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition
from app.loss.artifacts import LossArtifactCodec
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
from app.loss.validation import GridResidual
from tests.loss_factories import (
    building_damage_result,
    casualty_result,
    economic_loss_result,
    population_impact_result,
    resource_demand_result,
)


_LOSS_RASTER_NAMESPACE = "loss-raster-content-v1"
_TEST_GRID = GridDefinition(
    "loss-test-grid-v1",
    "EPSG:32651",
    1000,
    0.0,
    1000.0,
    1,
    1,
)
_TEST_FINGERPRINT = "f" * 64


@pytest.fixture(autouse=True)
async def clean_loss_repository_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(LossProductRaster))
            await session.execute(delete(LossMetricValue))
            await session.execute(delete(LossProduct))
    yield
    await engine.dispose()


async def _loss_product_write(
    session_factory,
    run_id,
    *,
    output_checksum: str = "a" * 64,
    numeric_value: float = 10.0,
    coverage_ratio: float = 1.0,
) -> LossProductWrite:
    async with session_factory() as session:
        task_id = await session.scalar(
            select(AssessmentTask.id)
            .where(AssessmentTask.run_id == run_id)
            .order_by(AssessmentTask.sequence, AssessmentTask.id)
        )
    if task_id is None:
        raise LookupError("seeded run has no assessment task")
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=LossProductType.BUILDING_DAMAGE,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L3,
        calibration_status=LossCalibrationStatus.UNCALIBRATED,
        coverage_ratio=coverage_ratio,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version="building-structure-matrix-v1",
        parameter_version="loss-test-only-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum=output_checksum,
        statistics={"town_count": 1},
        metrics=(
            LossMetricValueWrite(
                area_scope="city",
                area_code="310000",
                area_name="上海市",
                metric_key="building.damaged_area",
                value_type=LossValueType.CENTRAL,
                value_status=LossMetricValueStatus.AVAILABLE,
                numeric_value=numeric_value,
                unit="m2",
                precision=2,
                quality_grade=LossQualityGrade.L3,
                note=None,
            ),
        ),
        raster=None,
        reason=None,
    )


async def _task_ids(session_factory, run_id) -> dict[str, object]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AssessmentTask.task_key, AssessmentTask.id).where(
                    AssessmentTask.run_id == run_id
                )
            )
        ).all()
    return {task_key: task_id for task_key, task_id in rows}


def _model_results() -> dict[LossProductType, object]:
    return {
        LossProductType.BUILDING_DAMAGE: building_damage_result(),
        LossProductType.POPULATION_IMPACT: population_impact_result(),
        LossProductType.CASUALTIES: casualty_result(),
        LossProductType.ECONOMIC_LOSS: economic_loss_result(),
        LossProductType.RESOURCE_DEMAND: resource_demand_result(),
    }


def _building_raster_write() -> LossRasterWrite:
    values = np.asarray([[10.0]], dtype=np.float64)
    band_name = "collapsed_area_m2"
    manifest = {
        "grid": {
            "version": _TEST_GRID.version,
            "crs": _TEST_GRID.crs,
            "resolution_m": _TEST_GRID.resolution_m,
            "origin_x": _TEST_GRID.origin_x,
            "origin_y": _TEST_GRID.origin_y,
            "width": _TEST_GRID.width,
            "height": _TEST_GRID.height,
        },
        "bands": [
            {
                "number": 1,
                "name": band_name,
                "metric_key": band_name,
                "scenario": "central",
                "unit": "m2",
                "precision": 2,
            }
        ],
        "reconciliation": {
            "town": {
                "t1": {"residual": 0.0},
            }
        },
    }
    checksum = RasterCodec.content_checksum(
        _TEST_GRID,
        [(band_name, values)],
        manifest,
        checksum_namespace=_LOSS_RASTER_NAMESPACE,
    )
    return LossRasterWrite(
        raster_version="loss-test-grid-v1",
        definition=_TEST_GRID,
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


async def _model_product_write(
    session_factory,
    run_id,
    product_type: LossProductType,
    result: object,
    *,
    raster: LossRasterWrite | None = None,
) -> LossProductWrite:
    task_ids = await _task_ids(session_factory, run_id)
    task_key = {
        LossProductType.BUILDING_DAMAGE: "loss.buildings",
        LossProductType.POPULATION_IMPACT: "loss.population",
        LossProductType.CASUALTIES: "loss.casualties",
        LossProductType.ECONOMIC_LOSS: "loss.economic",
        LossProductType.RESOURCE_DEMAND: "loss.resources",
    }[product_type]
    task_id = task_ids.get(task_key)
    if task_id is None:
        task_id = next(iter(task_ids.values()))

    metrics: tuple[LossMetricValueWrite, ...] = ()
    if product_type is LossProductType.BUILDING_DAMAGE:
        metrics = (
            LossMetricValueWrite(
                area_scope="town",
                area_code="t1",
                area_name="测试镇",
                metric_key="collapsed_area_m2",
                value_type=LossValueType.CENTRAL,
                value_status=LossMetricValueStatus.AVAILABLE,
                numeric_value=10.0,
                unit="m2",
                precision=2,
                quality_grade=LossQualityGrade.L3,
                note=None,
            ),
        )

    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=product_type,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L3,
        calibration_status=LossCalibrationStatus.UNCALIBRATED,
        coverage_ratio=1.0,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=raster is not None,
        algorithm_version=f"{product_type.value}-v1",
        parameter_version="loss-test-only-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum="e" * 64,
        statistics={
            "data_asset_snapshot_fingerprint": _TEST_FINGERPRINT,
            "validation_payload": LossArtifactCodec.encode_validation_payload(
                product_type,
                result,
            ),
            "stage_seconds": {},
        },
        metrics=metrics,
        raster=raster,
        reason=None,
    )


async def test_product_write_is_idempotent_and_immutable(
    session_factory,
    seeded_assessment_run,
) -> None:
    write = await _loss_product_write(session_factory, seeded_assessment_run)
    repository = LossRepository()
    async with session_factory() as session:
        async with session.begin():
            first = await repository.write_product(session, write)
            second = await repository.write_product(session, write)
        assert first.id == second.id


async def test_changed_checksum_cannot_overwrite_existing_product(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    async with session_factory() as session:
        async with session.begin():
            write = await _loss_product_write(session_factory, seeded_assessment_run)
            await repository.write_product(session, write)
        with pytest.raises(ValueError, match="cannot be overwritten"):
            async with session.begin():
                await repository.write_product(
                    session,
                    await _loss_product_write(
                        session_factory,
                        seeded_assessment_run,
                        output_checksum="b" * 64,
                    ),
                )


async def test_decimal_metric_idempotency_after_database_rounding(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    write = await _loss_product_write(
        session_factory,
        seeded_assessment_run,
        numeric_value=0.1,
    )
    async with session_factory() as session:
        async with session.begin():
            first = await repository.write_product(session, write)
            second = await repository.write_product(session, write)
        assert first.id == second.id


async def test_small_changed_metric_is_rejected_after_database_rounding(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    first_write = await _loss_product_write(
        session_factory,
        seeded_assessment_run,
        numeric_value=0.1,
    )
    changed_write = await _loss_product_write(
        session_factory,
        seeded_assessment_run,
        numeric_value=0.1000009,
    )
    async with session_factory() as session:
        async with session.begin():
            await repository.write_product(session, first_write)
        with pytest.raises(ValueError, match="cannot be overwritten"):
            async with session.begin():
                await repository.write_product(session, changed_write)


async def test_small_changed_coverage_is_rejected_after_database_rounding(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    first_write = await _loss_product_write(
        session_factory,
        seeded_assessment_run,
        coverage_ratio=0.5,
    )
    changed_write = await _loss_product_write(
        session_factory,
        seeded_assessment_run,
        coverage_ratio=0.5000009,
    )
    async with session_factory() as session:
        async with session.begin():
            await repository.write_product(session, first_write)
        with pytest.raises(ValueError, match="cannot be overwritten"):
            async with session.begin():
                await repository.write_product(session, changed_write)


async def test_persisted_raster_round_trip_recovers_original_grid_version(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    raster = _building_raster_write()
    write = await _model_product_write(
        session_factory,
        seeded_assessment_run,
        LossProductType.BUILDING_DAMAGE,
        building_damage_result(),
        raster=raster,
    )
    async with session_factory() as session:
        async with session.begin():
            product = await repository.write_product(session, write)

    async with session_factory() as session:
        bands, metadata = await repository.load_raster(session, product.id)

    assert bands[0][0, 0] == pytest.approx(10.0)
    assert metadata["grid_definition_version"] == _TEST_GRID.version
    assert metadata["checksum"] == raster.checksum


async def test_malformed_persisted_raster_manifest_is_rejected(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    write = await _model_product_write(
        session_factory,
        seeded_assessment_run,
        LossProductType.BUILDING_DAMAGE,
        building_damage_result(),
        raster=_building_raster_write(),
    )
    async with session_factory() as session:
        async with session.begin():
            product = await repository.write_product(session, write)

    async with session_factory() as session:
        async with session.begin():
            raster_row = await session.scalar(
                select(LossProductRaster).where(
                    LossProductRaster.product_id == product.id
                )
            )
            malformed_manifest = dict(raster_row.band_manifest)
            malformed_manifest.pop("grid")
            raster_row.band_manifest = malformed_manifest

    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="grid metadata"):
            await repository.load_raster(session, product.id)


async def test_persisted_raster_checksum_is_recomputed_on_load(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    write = await _model_product_write(
        session_factory,
        seeded_assessment_run,
        LossProductType.BUILDING_DAMAGE,
        building_damage_result(),
        raster=_building_raster_write(),
    )
    async with session_factory() as session:
        async with session.begin():
            product = await repository.write_product(session, write)

    async with session_factory() as session:
        async with session.begin():
            raster_row = await session.scalar(
                select(LossProductRaster).where(
                    LossProductRaster.product_id == product.id
                )
            )
            raster_row.checksum = "0" * 64

    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="checksum verification failed"):
            await repository.load_raster(session, product.id)


async def test_load_validation_inputs_decodes_persisted_payloads_and_residuals(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    results = _model_results()
    writes = [
        await _model_product_write(
            session_factory,
            seeded_assessment_run,
            product_type,
            result,
            raster=(
                _building_raster_write()
                if product_type is LossProductType.BUILDING_DAMAGE
                else None
            ),
        )
        for product_type, result in results.items()
    ]

    async with session_factory() as session:
        async with session.begin():
            for write in writes:
                await repository.write_product(session, write)

    async with session_factory() as session:
        snapshot = await repository.load_validation_inputs(
            session,
            seeded_assessment_run,
        )

    assert snapshot.buildings == results[LossProductType.BUILDING_DAMAGE]
    assert snapshot.population == results[LossProductType.POPULATION_IMPACT]
    assert snapshot.casualties == results[LossProductType.CASUALTIES]
    assert snapshot.economic == results[LossProductType.ECONOMIC_LOSS]
    assert snapshot.resources == results[LossProductType.RESOURCE_DEMAND]
    assert snapshot.coverage_ratio == pytest.approx(1.0)
    assert snapshot.grid_residuals == (GridResidual("town", "t1", 0.0),)
    assert snapshot.product_snapshot_fingerprints == {
        product_type: _TEST_FINGERPRINT
        for product_type in results
    }
