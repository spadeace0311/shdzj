from dataclasses import replace

import pytest
from sqlalchemy import select

from app.assessment.models import AssessmentTask
from app.db import engine
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.instrument import UnavailableInstrumentProvider
from app.intensity.models import IntensityFieldProduct
from app.intensity.model import ModelFieldConvergenceError
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import DirectionInputs, IntensityService
from tests.intensity_helpers import (
    cleanup_intensity_fixture,
    seed_intensity_run,
)


@pytest.fixture(autouse=True)
async def reset_database_engine():
    await engine.dispose()
    yield
    await engine.dispose()


class FailingInstrumentProvider:
    async def fetch(self, request):
        raise RuntimeError("provider unavailable")


class ResolvedDirectionProvider:
    async def load(self, session, revision):
        return DirectionInputs(override_deg=0.0)


async def test_instrument_failure_does_not_fail_model_fusion_chain(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "failure-test-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=FailingInstrumentProvider(),
    )

    instrument_product = None
    try:
        model = await service.run_model(run_id)
        instrument = await service.run_instrument(run_id)
        fusion = await service.run_fusion(run_id)
        async with session_factory() as session:
            instrument_product = await session.scalar(
                select(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id == run_id,
                    IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
                )
            )
    finally:
        await cleanup_intensity_fixture(session_factory)

    assert model.status == "succeeded"
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"
    assert instrument_product is not None
    assert instrument_product.status == ProductStatus.UNAVAILABLE.value
    assert instrument_product.statistics["source"] == "provider_error"
    assert instrument_product.statistics["reason"] == "provider_error:RuntimeError"


async def test_duplicate_model_run_keeps_one_product(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "idempotency-test-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=UnavailableInstrumentProvider(),
    )

    try:
        first = await service.run_model(run_id)
        second = await service.run_model(run_id)
    finally:
        await cleanup_intensity_fixture(session_factory)

    assert first.product_id == second.product_id


async def test_model_failure_is_committed_to_task_audit(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    run_id = await seed_intensity_run(session_factory)
    parameters = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")
    service = IntensityService(
        session_factory=session_factory,
        parameters=replace(
            parameters,
            model=replace(
                parameters.model,
                solver_intensity_min=100.0,
                solver_intensity_max=101.0,
            ),
        ),
        fixed_grid_definition=GridDefinition(
            "failure-audit-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        direction_provider=ResolvedDirectionProvider(),
    )

    try:
        with pytest.raises(ModelFieldConvergenceError):
            await service.run_model(run_id)

        async with session_factory() as session:
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
        assert task is not None
        assert task.status == "failed"
        assert task.last_error is not None
    finally:
        await cleanup_intensity_fixture(session_factory)
