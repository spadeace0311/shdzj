from datetime import UTC, datetime
from decimal import Decimal

import pytest
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
from app.intensity.models import AssessmentTaskAttempt
from app.regions.domain import RegionContext


async def _delete(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AssessmentTaskAttempt))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))


@pytest.fixture(autouse=True)
async def clean_lifecycle_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _seed_run(session_factory) -> AssessmentRun:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="LIFECYCLE-1",
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
        raw_payload={"EventID": "LIFECYCLE-1", "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(True, Decimal("0"), "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            return await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )


async def test_task_attempts_are_idempotent(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "f" * 64,
            )
            second = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "f" * 64,
            )
            assert first.id == second.id
            assert first.attempt_count == 1

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(AssessmentTaskAttempt).where(
                    AssessmentTaskAttempt.task_id == first.id
                )
            )
        ).all()

    assert len(attempts) == 1


async def test_completed_run_sets_deadline_and_effective_pointer(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    assert run.deadline_basis_at == datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    assert run.deadline_at == datetime(2026, 9, 27, 1, 8, tzinfo=UTC)

    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await repository.complete_task(
                session,
                task.id,
                "a" * 64,
                {"product_id": "p"},
            )
            fusion = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            await repository.complete_task(
                session,
                fusion.id,
                "f" * 64,
                {"product_id": "fusion"},
            )
            completed = await repository.complete_run(
                session,
                run.id,
                "bundle-1",
            )
            event = await session.get(EarthquakeEvent, run.event_id)

    assert completed.status == "completed"
    assert event.effective_assessment_run_id == run.id
