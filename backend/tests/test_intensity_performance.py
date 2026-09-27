import time

import pytest

from app.db import engine
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from tests.intensity_helpers import (
    cleanup_intensity_fixture,
    seed_intensity_boundary,
    seed_intensity_run,
)


@pytest.fixture(autouse=True)
async def reset_database_engine():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.performance
async def test_full_shanghai_grid_meets_intensity_budget(
    session_factory,
) -> None:
    await cleanup_intensity_fixture(session_factory)
    boundary_version = await seed_intensity_boundary(session_factory)
    run_id = await seed_intensity_run(
        session_factory,
        boundary_version=boundary_version,
    )
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
    )

    try:
        started = time.perf_counter()
        model = await service.run_model(run_id)
        model_seconds = time.perf_counter() - started

        instrument_started = time.perf_counter()
        await service.run_instrument(run_id)
        instrument_seconds = time.perf_counter() - instrument_started

        fusion_started = time.perf_counter()
        fusion = await service.run_fusion(run_id)
        fusion_seconds = time.perf_counter() - fusion_started
    finally:
        await cleanup_intensity_fixture(session_factory)

    print(
        "intensity performance model={:.3f}s instrument={:.3f}s fusion={:.3f}s".format(
            model_seconds,
            instrument_seconds,
            fusion_seconds,
        )
    )
    assert model.status == "succeeded"
    assert fusion.status == "succeeded"
    assert model_seconds <= 60.0
    assert instrument_seconds <= 60.0
    assert fusion_seconds <= 90.0
