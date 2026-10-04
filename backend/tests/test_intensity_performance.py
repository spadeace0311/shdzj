from datetime import UTC, datetime
import time

import numpy as np
import pytest
from sqlalchemy import select

from app.db import engine
from app.intensity.domain import (
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ProductStatus,
    ProductType,
)
from app.intensity.models import IntensityFieldProduct
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


class FullGridInstrumentProvider:
    async def fetch(self, request):
        shape = (request.definition.height, request.definition.width)
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="full-grid-instrument",
            product_version="1",
            observed_at=datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
            source="benchmark-grid",
            format=InstrumentProductFormat.GRID,
            source_verified=True,
            grid_version=request.definition.version,
            values=np.full(shape, 5.0, dtype=np.float64),
            sigma=np.full(shape, 0.2, dtype=np.float64),
            quality_codes=np.full(shape, InstrumentQuality.Q1, dtype=object),
            coverage_ratio=1.0,
        )


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
        instrument_provider=FullGridInstrumentProvider(),
    )

    instrument_product = None
    try:
        started = time.perf_counter()
        model = await service.run_model(run_id)
        model_seconds = time.perf_counter() - started

        instrument_started = time.perf_counter()
        instrument = await service.run_instrument(run_id)
        instrument_seconds = time.perf_counter() - instrument_started

        fusion_started = time.perf_counter()
        fusion = await service.run_fusion(run_id)
        fusion_seconds = time.perf_counter() - fusion_started

        async with session_factory() as session:
            instrument_product = await session.scalar(
                select(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id == run_id,
                    IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
                )
            )
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
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"
    assert instrument_product is not None
    assert instrument_product.status == ProductStatus.AVAILABLE.value
    assert instrument_product.statistics["source"] == "benchmark-grid"
    assert model_seconds <= 60.0
    assert instrument_seconds <= 60.0
    assert fusion_seconds <= 90.0
