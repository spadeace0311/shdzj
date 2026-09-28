from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_assessment_data(session_factory):
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    yield
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
    await engine.dispose()


def _collected_formal() -> dict[str, object]:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-ASSESSMENT-1",
        origin_time=datetime(2026, 9, 26, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 1, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 1, 3, tzinfo=UTC)
    return {
        "raw_payload": {
            "EventID": event.source_event_id,
            "type": "reviewed",
            "magnitude": "5.2",
        },
        "event": event,
        "provider": "fan",
        "lane": "websocket",
        "received_at": received_at,
        "response_input": ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        "region_context": RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="test-2026.1",
            computed_at=received_at,
        ),
    }


async def _persisted_assessment_trigger(session_factory):
    outcome = await EventService(session_factory).ingest_collected(**_collected_formal())

    async with session_factory() as session:
        event = await session.get(EarthquakeEvent, outcome.event_id)
        revision = await session.get(EarthquakeRevision, outcome.revision_id)
        outbox = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.revision_id == outcome.revision_id,
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )

    assert event is not None
    assert revision is not None
    assert outbox is not None
    return event, revision, outbox


async def test_repository_creates_run_and_nine_tasks_idempotently(session_factory) -> None:
    event, revision, outbox = await _persisted_assessment_trigger(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await repository.ensure_run_and_tasks(
                session,
                event=event,
                revision=revision,
                outbox=outbox,
            )

    async with session_factory() as session:
        async with session.begin():
            second = await repository.ensure_run_and_tasks(
                session,
                event=event,
                revision=revision,
                outbox=outbox,
            )

        task_count = await session.scalar(
            select(func.count())
            .select_from(AssessmentTask)
            .where(AssessmentTask.run_id == first.id)
        )

    assert first.id == second.id
    assert first.run_no == 1
    assert first.t1_at == datetime(2026, 9, 26, 1, 3, tzinfo=UTC)
    assert first.deadline_at == datetime(2026, 9, 26, 1, 8, tzinfo=UTC)
    assert first.report_ingested_at == first.t1_at
    assert first.deadline_basis_at == first.t1_at
    assert first.snapshot["event_id"] == str(event.id)
    assert first.snapshot["revision_id"] == str(revision.id)
    assert first.snapshot["revision_no"] == revision.revision_no
    assert first.snapshot["t1_at"] == "2026-09-26T01:03:00+00:00"
    assert first.snapshot["response_rule_version"] == revision.response_rule_version
    assert first.snapshot["region_boundary_version"] == "test-2026.1"
    assert first.snapshot["region_id"] == settings.data_asset_region_id
    assert first.snapshot["data_asset_snapshot"]["missing_required"]
    assert first.data_asset_snapshot_fingerprint
    assert task_count == 9


async def test_repository_increments_run_number_for_each_trigger(session_factory) -> None:
    first_event, first_revision, first_outbox = await _persisted_assessment_trigger(
        session_factory
    )
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await repository.ensure_run_and_tasks(
                session,
                event=first_event,
                revision=first_revision,
                outbox=first_outbox,
            )

    correction = _collected_formal()
    correction_event = correction["event"]
    assert isinstance(correction_event, NormalizedEvent)
    correction["event"] = replace(
        correction_event,
        kind=EventKind.CORRECTION,
        magnitude=Decimal("5.3"),
        report_time=datetime(2026, 9, 26, 1, 4, tzinfo=UTC),
    )
    correction["received_at"] = datetime(2026, 9, 26, 1, 5, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(**correction)

    async with session_factory() as session:
        async with session.begin():
            event = await session.get(EarthquakeEvent, outcome.event_id)
            revision = await session.get(EarthquakeRevision, outcome.revision_id)
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id,
                )
            )
            assert event is not None
            assert revision is not None
            assert outbox is not None
            second = await repository.ensure_run_and_tasks(
                session,
                event=event,
                revision=revision,
                outbox=outbox,
            )

    assert first.run_no == 1
    assert second.run_no == 2
