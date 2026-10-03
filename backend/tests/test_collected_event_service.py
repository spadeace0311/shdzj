import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.collector.domain import CollectorLane, CollectorProvider
from app.db import SessionFactory, engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.repository import EventRepository
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.events.sources.cenc import CencAdapter
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_lifecycle_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    await engine.dispose()


def collected_event(
    *,
    kind: str = "formal",
    magnitude: str = "5.1",
    provider: str = "fan",
    lane: str = "websocket",
    report_time: str = "2026-09-25T01:04:00Z",
    report_number: int | None = None,
) -> dict[str, object]:
    event = NormalizedEvent(
        kind=EventKind(kind),
        source="cenc",
        source_event_id="CENC-2026-0001",
        origin_time=datetime(2026, 9, 25, 1, 2, 3, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="上海测试位置",
        report_time=datetime.fromisoformat(report_time.replace("Z", "+00:00")),
        report_number=report_number,
    )
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    region_context = RegionContext(
        inside_shanghai=True,
        distance_to_boundary_km=Decimal("0"),
        boundary_version="test-2026.1",
        computed_at=received_at,
    )
    return {
        "raw_payload": {
            "eventId": "CENC-2026-0001",
            "reportType": kind,
            "originTime": event.origin_time.isoformat(),
            "longitude": str(event.longitude),
            "latitude": str(event.latitude),
            "magnitude": str(event.magnitude),
            "depth": str(event.depth_km),
            "place": event.place,
        },
        "event": event,
        "provider": CollectorProvider(provider).value,
        "lane": CollectorLane(lane).value,
        "received_at": received_at,
        "response_input": ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        "region_context": region_context,
    }


def collected_service(session_factory=SessionFactory) -> EventService:
    return EventService(session_factory)


async def outbox_count(session_factory, event_id: str) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(EventLifecycleOutbox)
                .where(
                    EventLifecycleOutbox.event_id == event_id,
                    EventLifecycleOutbox.trigger_type
                    == "assessment.requested",
                )
            )
            or 0
        )


async def collaboration_outbox_count(
    session_factory,
    event_id: str,
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(EventLifecycleOutbox)
                .where(
                    EventLifecycleOutbox.event_id == event_id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )
            or 0
        )


async def outbox_reason(session_factory, revision_id: str) -> str | None:
    async with session_factory() as session:
        return await session.scalar(
            select(EventLifecycleOutbox.trigger_reason).where(
                EventLifecycleOutbox.revision_id == revision_id
            )
        )


async def test_auto_then_formal_creates_one_event_and_one_outbox(session_factory) -> None:
    service = collected_service(session_factory)
    auto = collected_event(kind="auto", magnitude="4.8", report_time="2026-09-25T01:02:00Z")
    formal = collected_event(kind="formal", magnitude="5.1", report_time="2026-09-25T01:04:00Z")

    auto_result = await service.ingest_collected(**auto)
    formal_result = await service.ingest_collected(**formal)

    assert auto_result.triggered_assessment is False
    assert formal_result.triggered_assessment is True
    assert formal_result.event_id == auto_result.event_id
    assert formal_result.t1_at == formal["received_at"]
    assert await outbox_count(session_factory, auto_result.event_id) == 1
    assert (
        await collaboration_outbox_count(
            session_factory,
            auto_result.event_id,
        )
        == 1
    )


async def test_equivalent_fan_and_wolfx_reviewed_messages_do_not_duplicate(session_factory) -> None:
    service = collected_service(session_factory)
    fan_payload = {
        "id": "CENC-2026-0001",
        "infoTypeName": "正式(已核实)",
        "shockTime": "2026/09/25 09:02:03",
        "createTime": "2026/09/25 09:04:00",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    wolfx_payload = {
        "type": "reviewed",
        "EventID": "CENC-2026-0001",
        "time": "2026/09/25 09:02:03",
        "ReportTime": "2026/09/25 09:04:00",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }
    fan = {
        **collected_event(kind="formal", provider="fan", lane="websocket"),
        "event": CencAdapter().parse(fan_payload),
        "raw_payload": fan_payload,
    }
    wolfx = {
        **collected_event(kind="formal", provider="wolfx", lane="http"),
        "event": CencAdapter().parse(wolfx_payload),
        "raw_payload": wolfx_payload,
    }

    first = await service.ingest_collected(**fan)
    second = await service.ingest_collected(**wolfx)

    assert first.revision_id == second.revision_id
    assert first.is_new is True
    assert second.is_new is False
    assert await outbox_count(session_factory, first.event_id) == 1
    assert await collaboration_outbox_count(session_factory, first.event_id) == 1


async def test_changed_reviewed_message_creates_correction_and_second_outbox(session_factory) -> None:
    service = collected_service(session_factory)
    formal = collected_event(kind="formal", magnitude="5.1")
    correction = collected_event(
        kind="formal",
        magnitude="5.2",
        report_time="2026-09-25T01:06:00Z",
    )

    first = await service.ingest_collected(**formal)
    second = await service.ingest_collected(**correction)

    assert second.event_kind.value == "correction"
    assert second.t1_at == first.t1_at
    assert second.triggered_assessment is True
    assert await outbox_count(session_factory, first.event_id) == 2
    assert (
        await collaboration_outbox_count(
            session_factory,
            first.event_id,
        )
        == 2
    )


async def test_recovery_trigger_reason_is_persisted(session_factory) -> None:
    service = collected_service(session_factory)
    result = await service.ingest_collected(
        **collected_event(kind="formal"),
        trigger_reason="recovery",
    )

    assert await outbox_reason(session_factory, result.revision_id) == "recovery"


async def test_first_reviewed_correction_is_classified_as_formal(session_factory) -> None:
    service = collected_service(session_factory)
    result = await service.ingest_collected(**collected_event(kind="correction"))

    assert result.event_kind is EventKind.FORMAL
    assert result.lifecycle_state == "formal_triggered"
    assert result.t1_at is not None
    assert result.triggered_assessment is True
    assert await outbox_count(session_factory, result.event_id) == 1
    assert (
        await collaboration_outbox_count(
            session_factory,
            result.event_id,
        )
        == 1
    )

    async with session_factory() as session:
        event = await session.get(EarthquakeEvent, uuid.UUID(result.event_id))
        revision = await session.get(EarthquakeRevision, uuid.UUID(result.revision_id))

    assert event is not None
    assert revision is not None
    assert event.event_type == "formal"
    assert revision.revision_kind == "formal"


async def test_enqueue_assessment_is_guarded_noop(session_factory) -> None:
    service = collected_service(session_factory)
    result = await service.ingest_collected(**collected_event(kind="formal"))
    repository = EventRepository(session_factory)

    async with session_factory() as session:
        async with session.begin():
            added = await repository.enqueue_assessment(
                session,
                event_id=result.event_id,
                revision_id=result.revision_id,
                revision_no=result.revision_no,
                trigger_reason="live",
                created_at=result.t1_at or datetime.now(UTC),
            )

    assert added is False
    assert await outbox_count(session_factory, result.event_id) == 1
    assert (
        await collaboration_outbox_count(
            session_factory,
            result.event_id,
        )
        == 1
    )


async def test_late_older_reviewed_revision_is_stored_without_new_outbox(
    session_factory,
) -> None:
    service = collected_service(session_factory)
    current = collected_event(report_number=3)
    stale = collected_event(
        magnitude="5.0",
        report_number=2,
        report_time="2026-09-25T01:03:00Z",
    )

    first = await service.ingest_collected(**current)
    second = await service.ingest_collected(**stale)

    assert second.is_new is True
    assert second.is_current is False
    assert second.triggered_assessment is False
    assert await outbox_count(session_factory, first.event_id) == 1
    assert await collaboration_outbox_count(session_factory, first.event_id) == 1
