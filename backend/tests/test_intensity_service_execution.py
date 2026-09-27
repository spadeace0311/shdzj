from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import (
    FusionMode,
    FusionQuality,
    GridDefinition,
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ProductStatus,
    ProductType,
)
from app.intensity.models import (
    AssessmentTaskAttempt,
    IntensityFieldProduct,
    IntensityRaster,
)
from app.intensity.parameters import load_parameter_bundle
from app.intensity.region import load_region_profile
from app.intensity.repository import IntensityRepository
from app.intensity.service import IntensityService
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary


class UnavailableProvider:
    async def fetch(self, request):
        return InstrumentProduct(
            status=ProductStatus.UNAVAILABLE,
            product_id=None,
            product_version=None,
            observed_at=None,
            source="unavailable",
            format=None,
            source_verified=False,
            grid_version=request.definition.version,
            values=None,
            sigma=None,
            quality_codes=None,
            coverage_ratio=0.0,
        )


class InvalidGridProvider:
    async def fetch(self, request):
        bad_shape = (request.definition.height + 1, request.definition.width)
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="invalid-instrument",
            product_version="1",
            observed_at=datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
            source="malformed",
            format=InstrumentProductFormat.GRID,
            source_verified=True,
            grid_version=request.definition.version,
            values=np.zeros(bad_shape, dtype=np.float64),
            sigma=np.zeros(bad_shape, dtype=np.float64),
            quality_codes=np.full(bad_shape, InstrumentQuality.Q1, dtype=object),
            coverage_ratio=1.0,
        )


class FailingInstrumentProvider:
    async def fetch(self, request):
        raise RuntimeError("provider unavailable")


class ValidGridProvider:
    async def fetch(self, request):
        shape = (request.definition.height, request.definition.width)
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="valid-instrument",
            product_version="1",
            observed_at=datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
            source="verified-grid",
            format=InstrumentProductFormat.GRID,
            source_verified=True,
            grid_version=request.definition.version,
            values=np.full(shape, 5.0, dtype=np.float64),
            sigma=np.full(shape, 0.2, dtype=np.float64),
            quality_codes=np.full(shape, InstrumentQuality.Q1, dtype=object),
            coverage_ratio=1.0,
        )


@pytest.fixture(autouse=True)
async def clean_execution_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTaskAttempt))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


async def _seed_run(
    session_factory,
    *,
    boundary_version: str = "grid-test",
) -> str:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="EXECUTION-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "EXECUTION-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=0,
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(True, 0, boundary_version, received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return str(run.id)


async def test_three_tasks_produce_model_only_fusion(session_factory) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-test",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=UnavailableProvider(),
    )

    model = await service.run_model(run_id)
    instrument = await service.run_instrument(run_id)
    fusion = await service.run_fusion(run_id)

    assert model.status == "succeeded"
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"

    async with session_factory() as session:
        fusion_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == model.run_id,
                IntensityFieldProduct.product_type == ProductType.FUSION.value,
            )
        )

    assert fusion_product is not None
    assert fusion_product.status == "available"
    assert fusion_product.quality_grade == FusionQuality.F3.value
    assert fusion_product.statistics["fusion_mode"] == FusionMode.MODEL_ONLY.value


async def test_invalid_instrument_is_persisted_and_fusion_falls_back(
    session_factory,
) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-invalid-instrument",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=InvalidGridProvider(),
    )

    assert (await service.run_model(run_id)).status == "succeeded"
    instrument = await service.run_instrument(run_id)
    fusion = await service.run_fusion(run_id)

    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"

    async with session_factory() as session:
        product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
            )
        )
        fusion_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == ProductType.FUSION.value,
            )
        )

    assert product is not None
    assert product.status == ProductStatus.INVALID.value
    assert "shape" in product.statistics["reason"]
    assert fusion_product is not None
    assert fusion_product.quality_grade == FusionQuality.F3.value
    assert fusion_product.statistics["fusion_mode"] == FusionMode.MODEL_ONLY.value


async def test_provider_error_persists_unavailable_and_fusion_falls_back(
    session_factory,
) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-provider-error",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=FailingInstrumentProvider(),
    )

    assert (await service.run_model(run_id)).status == "succeeded"
    instrument = await service.run_instrument(run_id)
    fusion = await service.run_fusion(run_id)

    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"

    async with session_factory() as session:
        product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
            )
        )

    assert product is not None
    assert product.status == ProductStatus.UNAVAILABLE.value
    assert product.statistics["reason"] == "provider_error:RuntimeError"


async def test_valid_instrument_runs_full_fusion_and_preserves_checksum(
    session_factory,
) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-valid-instrument",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=ValidGridProvider(),
    )

    assert (await service.run_model(run_id)).status == "succeeded"
    assert (await service.run_instrument(run_id)).status == "succeeded"
    fusion = await service.run_fusion(run_id)

    assert fusion.status == "succeeded"

    async with session_factory() as session:
        instrument_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
            )
        )
        fusion_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == ProductType.FUSION.value,
            )
        )

    assert instrument_product is not None
    assert instrument_product.status == ProductStatus.AVAILABLE.value
    assert len(instrument_product.statistics["normalized_checksum"]) == 64
    assert fusion_product is not None
    assert fusion_product.statistics["fusion_mode"] == FusionMode.FULL.value
    assert fusion_product.quality_grade == FusionQuality.F1.value


async def test_task_failure_audit_preserves_algorithm_and_fingerprint(
    session_factory,
) -> None:
    run_id = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "grid-audit",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )

    async with session_factory() as session:
        async with session.begin():
            await service.assessment_repository.start_task(
                session,
                run_id,
                "intensity.model",
                service.MODEL_ALGORITHM_VERSION,
                "f" * 64,
            )

    await service._record_task_failure(
        run_id,
        "intensity.model",
        RuntimeError("boom"),
    )

    async with session_factory() as session:
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == "intensity.model",
            )
        )
        attempt = await session.scalar(
            select(AssessmentTaskAttempt).where(
                AssessmentTaskAttempt.task_id == task.id,
                AssessmentTaskAttempt.attempt_number == 1,
            )
        )

    assert task.status == "failed"
    assert task.algorithm_version == service.MODEL_ALGORITHM_VERSION
    assert task.input_fingerprint == "f" * 64
    assert attempt is not None
    assert attempt.error_category == "RuntimeError"
    assert attempt.input_fingerprint == "f" * 64


async def test_repository_resolves_real_grid_definition(session_factory) -> None:
    boundary_version = f"intensity-grid-{uuid4()}"
    geometry = WKTElement(
        "MULTIPOLYGON (((120.8 30.6, 121.2 30.6, 121.2 31.0, "
        "120.8 31.0, 120.8 30.6)))",
        srid=4326,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    RegionBoundary(
                        version=boundary_version,
                        name="intensity execution grid",
                        local_buffer_km=Decimal("50"),
                        geom=geometry,
                        maritime_geom=geometry,
                        source_uri="test://intensity-execution-grid",
                        checksum="a" * 64,
                        is_active=False,
                    )
                )

        profile = load_region_profile("/config/intensity/shanghai-region.yaml")
        async with session_factory() as session:
            definition = await IntensityRepository().resolve_grid_definition(
                session,
                profile,
                boundary_version,
            )
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(RegionBoundary).where(
                        RegionBoundary.version == boundary_version
                    )
                )

    assert definition.version.startswith(
        f"{profile.version}:{boundary_version}:"
    )
    assert definition.crs == "EPSG:32651"
    assert definition.resolution_m == 1000
    assert definition.origin_x % 1000 == 0
    assert definition.origin_y % 1000 == 0
    assert definition.width > 0
    assert definition.height > 0
