from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary


BOUNDARY_VERSION_PREFIX = "intensity-performance-"
EVENT_PREFIX = "INTENSITY-PERF-"


async def cleanup_intensity_fixture(session_factory) -> None:
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
            await session.execute(
                delete(RegionBoundary).where(
                    RegionBoundary.version.like(f"{BOUNDARY_VERSION_PREFIX}%")
                )
            )


async def seed_intensity_boundary(session_factory) -> str:
    version = f"{BOUNDARY_VERSION_PREFIX}{uuid4()}"
    geometry = WKTElement(
        "MULTIPOLYGON (((120.8 30.6, 122.2 30.6, 122.2 31.9, "
        "120.8 31.9, 120.8 30.6)))",
        srid=4326,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegionBoundary(
                    version=version,
                    name="intensity performance test",
                    local_buffer_km=Decimal("50"),
                    geom=geometry,
                    maritime_geom=geometry,
                    source_uri="test://intensity-performance",
                    checksum="a" * 64,
                    is_active=False,
                )
            )
    return version


async def seed_intensity_run(
    session_factory,
    *,
    boundary_version: str = "intensity-test-grid",
) -> str:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id=f"{EVENT_PREFIX}{uuid4()}",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="intensity test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
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
