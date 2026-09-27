from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_intensity_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    await engine.dispose()


async def _seed_run(session_factory) -> tuple[UUID, UUID]:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="INTENSITY-REPO-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
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
            boundary_version="test-grid",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            from app.assessment.repository import AssessmentRepository

            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            return run.id, task.id


async def test_product_and_raster_round_trip(session_factory) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    definition = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=definition,
        region_profile_version="shanghai-v1",
        input_fingerprint="f" * 64,
        input_checksum="i" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        product = await repository.get_product(
            session,
            run_id=run_id,
            product_type=ProductType.MODEL,
        )
        bands, metadata = await repository.load_raster(session, product_id)

    assert product is not None
    assert product["product_type"] == "model"
    assert bands[0][0, 0] == pytest.approx(4.0)
    assert metadata["grid_definition_version"] == "grid-1"
