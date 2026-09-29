import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentTask
from app.db import engine
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
    LossRepository,
)


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
        coverage_ratio=1.0,
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
                numeric_value=10.0,
                unit="m2",
                precision=2,
                quality_grade=LossQualityGrade.L3,
                note=None,
            ),
        ),
        raster=None,
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
