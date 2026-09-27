from datetime import UTC, datetime

import numpy as np

from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ProductStatus,
)
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService


class FakeInstrumentProvider:
    async def fetch(self, request):
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="instrument-fixture",
            product_version="1",
            observed_at=datetime(2026, 9, 27, tzinfo=UTC),
            source="fixture",
            format=InstrumentProductFormat.GRID,
            source_verified=True,
            grid_version=request.definition.version,
            values=np.full((request.definition.height, request.definition.width), 5.0),
            sigma=np.full((request.definition.height, request.definition.width), 0.2),
            quality_codes=np.full(
                (request.definition.height, request.definition.width),
                InstrumentQuality.Q1,
                dtype=object,
            ),
            coverage_ratio=1.0,
        )


def test_service_exposes_injected_grid_and_provider() -> None:
    definition = GridDefinition("grid-test", "EPSG:32651", 1000, 0, 1000, 1, 1)
    service = IntensityService(
        session_factory=object(),
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=definition,
        instrument_provider=FakeInstrumentProvider(),
    )

    assert service.grid_definition.version == "grid-test"
