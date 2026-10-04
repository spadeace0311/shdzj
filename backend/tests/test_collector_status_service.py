import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete

from app.collector.models import CollectorDeadLetter, CollectorRuntimeState
from app.collector.status_service import CollectorStatusService
from app.db import engine
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.regions.importer import import_geojson
from app.regions.models import RegionBoundary
from app.regions.repository import RegionRepository


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shanghai_boundary.geojson"
BOUNDARY_VERSION = "status-2026.1"


@pytest.fixture(autouse=True)
async def clean_collector_status_data(session_factory):
    await engine.dispose()

    async def clean() -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(CollectorDeadLetter))
                await session.execute(delete(CollectorRuntimeState))
                await session.execute(delete(EarthquakeRevision))
                await session.execute(delete(EarthquakeEvent))
                await session.execute(delete(RawMessage))
                await session.execute(delete(RegionBoundary))

    await clean()
    yield
    await clean()
    await engine.dispose()


def _runtime(
    provider: str,
    state: str,
    *,
    connected: bool,
    updated_at: datetime,
) -> CollectorRuntimeState:
    return CollectorRuntimeState(
        provider=provider,
        state=state,
        connected=connected,
        last_http_status=200 if provider == "wolfx" else None,
        last_connected_at=updated_at,
        last_message_at=updated_at,
        last_success_at=updated_at,
        consecutive_failures=0,
        reconnect_count=1,
        last_error=None,
        updated_at=updated_at,
    )


async def _add_revision(
    session,
    *,
    event_id: uuid.UUID,
    ingested_at: datetime | None,
    revision_no: int,
) -> None:
    raw_message = RawMessage(
        id=uuid.uuid4(),
        source="cenc",
        source_message_id=str(event_id),
        message_kind="formal",
        provider="wolfx",
        ingest_lane="http",
        received_at=datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
        checksum=uuid.uuid4().hex + uuid.uuid4().hex,
        payload={"EventID": str(event_id)},
    )
    event = EarthquakeEvent(
        id=event_id,
        source="cenc",
        canonical_source_id=f"cenc:{event_id}",
        event_type="formal",
        origin_time=datetime(2026, 9, 25, 1, 2, 3, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
    )
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event_id,
        raw_message_id=raw_message.id,
        revision_no=revision_no,
        revision_kind="formal",
        source_event_id=str(event_id),
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        provider="wolfx",
        ingest_lane="http",
        ingested_at=ingested_at,
        is_current=False,
    )
    session.add_all([raw_message, event, revision])
    await session.flush()


async def test_status_service_aggregates_collector_state_from_one_session(
    session_factory,
) -> None:
    updated_at = datetime(2026, 9, 25, 1, 5, 10, tzinfo=UTC)
    latest_event_id = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    _runtime("fan", "healthy", connected=True, updated_at=updated_at),
                    _runtime("wolfx", "degraded", connected=False, updated_at=updated_at),
                    CollectorDeadLetter(
                        id=uuid.uuid4(),
                        provider="wolfx",
                        lane="http",
                        raw_payload={"No1": {"EventID": "open"}},
                        received_at=updated_at,
                        error_category="parse_error",
                        error_message="invalid",
                        status="open",
                        first_failed_at=updated_at,
                        last_failed_at=updated_at,
                    ),
                    CollectorDeadLetter(
                        id=uuid.uuid4(),
                        provider="wolfx",
                        lane="http",
                        raw_payload={"No1": {"EventID": "resolved"}},
                        received_at=updated_at,
                        error_category="parse_error",
                        error_message="fixed",
                        status="resolved",
                        first_failed_at=updated_at,
                        last_failed_at=updated_at,
                    ),
                ]
            )
            await import_geojson(
                session,
                FIXTURE_PATH,
                version=BOUNDARY_VERSION,
                name="status-shanghai",
                source_uri="https://example.gov.invalid/status.geojson",
                activate=True,
            )
            older_event_id = uuid.uuid4()
            await _add_revision(
                session,
                event_id=older_event_id,
                ingested_at=datetime(2026, 9, 25, 1, 4, tzinfo=UTC),
                revision_no=1,
            )
            await _add_revision(
                session,
                event_id=latest_event_id,
                ingested_at=datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
                revision_no=1,
            )
            await _add_revision(
                session,
                event_id=uuid.uuid4(),
                ingested_at=None,
                revision_no=99,
            )

    service = CollectorStatusService(session_factory, RegionRepository(session_factory))
    status = await service.get_status()

    assert status.overall_state == "healthy"
    assert [provider.provider for provider in status.providers] == ["fan", "wolfx"]
    assert status.providers[0].last_connected_at == updated_at
    assert status.providers[1].last_http_status == 200
    assert status.open_dead_letter_count == 1
    assert status.boundary_version == BOUNDARY_VERSION
    assert status.last_ingested_event_id == str(latest_event_id)


@pytest.mark.parametrize(
    ("fan_state", "wolfx_state", "expected"),
    [
        ("degraded", "healthy", "degraded"),
        ("critical", "critical", "critical"),
        (None, None, "critical"),
    ],
)
async def test_status_service_overall_state_handles_degraded_and_missing_rows(
    session_factory,
    fan_state: str | None,
    wolfx_state: str | None,
    expected: str,
) -> None:
    updated_at = datetime(2026, 9, 25, 1, 5, 10, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            if fan_state is not None:
                session.add(_runtime("fan", fan_state, connected=False, updated_at=updated_at))
            if wolfx_state is not None:
                session.add(_runtime("wolfx", wolfx_state, connected=False, updated_at=updated_at))

    service = CollectorStatusService(session_factory, RegionRepository(session_factory))
    status = await service.get_status()

    assert status.overall_state == expected
