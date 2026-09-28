from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
)
from app.db import engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.service import EventService
from app.regions.domain import RegionContext

_INTEGRATION_SOURCE_IDS = ("CENC-INT-1", "CENC-BACKUP-ONLY-1")


@pytest.fixture(autouse=True)
async def isolate_integration_events(session_factory):
    await engine.dispose()
    await _delete_integration_events(session_factory)
    try:
        yield
    finally:
        await _delete_integration_events(session_factory)
        await engine.dispose()


async def _delete_integration_events(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            event_ids = list(
                await session.scalars(
                    select(EarthquakeEvent.id).where(
                        EarthquakeEvent.canonical_source_id.in_(
                            f"cenc:{source_id}" for source_id in _INTEGRATION_SOURCE_IDS
                        )
                    )
                )
            )
            if event_ids:
                await session.execute(
                    delete(EventLifecycleOutbox).where(
                        EventLifecycleOutbox.event_id.in_(event_ids)
                    )
                )
                await session.execute(
                    delete(EarthquakeRevision).where(
                        EarthquakeRevision.event_id.in_(event_ids)
                    )
                )
                await session.execute(
                    delete(EarthquakeEvent).where(EarthquakeEvent.id.in_(event_ids))
                )
            await session.execute(
                delete(RawMessage).where(
                    RawMessage.source_message_id.in_(_INTEGRATION_SOURCE_IDS)
                )
            )


class FixedRegionResolver:
    async def resolve(self, longitude, latitude) -> RegionContext:
        return RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=0,
            boundary_version="test-2026.1",
            computed_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        )


async def test_fan_auto_then_wolfx_formal_creates_one_trigger(session_factory) -> None:
    service = EventService(session_factory)
    coordinator = CollectorCoordinator(service, FixedRegionResolver())
    auto = {
        "EventID": "CENC-INT-1",
        "type": "automatic",
        "time": "2026-09-25T01:02:03Z",
        "placeName": "上海集成测试",
        "magnitude": 4.8,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    formal = {
        **auto,
        "type": "reviewed",
        "ReportTime": "2026-09-25T01:04:00Z",
        "magnitude": 5.1,
    }
    formal_received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)

    auto_outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 3, tzinfo=UTC),
            payload={"No1": auto},
        )
    )
    formal_outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=formal_received_at,
            payload={"No1": formal},
        ),
        trigger_reason="recovery",
    )

    assert auto_outcome is not None
    assert formal_outcome is not None
    assert auto_outcome.event_id == formal_outcome.event_id
    assert auto_outcome.triggered_assessment is False
    assert formal_outcome.triggered_assessment is True
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(EventLifecycleOutbox)
            .where(
                EventLifecycleOutbox.event_id == UUID(formal_outcome.event_id),
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )
        t1_at = await session.scalar(
            select(EarthquakeEvent.t1_at).where(
                EarthquakeEvent.id == UUID(formal_outcome.event_id)
            )
        )
    assert count == 1
    assert t1_at == formal_received_at


async def test_wolfx_formal_reaches_lifecycle_when_fan_is_unavailable(
    session_factory,
) -> None:
    service = EventService(session_factory)
    coordinator = CollectorCoordinator(service, FixedRegionResolver())
    formal = {
        "type": "reviewed",
        "EventID": "CENC-BACKUP-ONLY-1",
        "time": "2026-09-25T02:02:03Z",
        "ReportTime": "2026-09-25T02:04:00Z",
        "placeName": "上海备用链路集成测试",
        "magnitude": 5.0,
        "depth": 10,
        "latitude": 31.25,
        "longitude": 121.55,
    }

    outcome = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 2, 5, tzinfo=UTC),
            payload={"No1": formal},
        )
    )

    assert outcome is not None
    assert outcome.triggered_assessment is True
    assert outcome.lifecycle_state == "formal_triggered"
