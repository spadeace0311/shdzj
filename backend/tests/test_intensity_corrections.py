from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_correction_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory):
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


async def _complete_loss_tasks(session, run_id) -> None:
    repository = AssessmentRepository()
    for task_key, algorithm_version, fingerprint in (
        ("loss.population", "population-intensity-v1", "p" * 64),
        ("loss.buildings", "building-structure-matrix-v1", "b" * 64),
        ("loss.casualties", "casualty-building-intensity-v1", "c" * 64),
        ("loss.economic", "economic-building-loss-v1", "e" * 64),
        ("loss.resources", "resource-linear-demand-v1", "r" * 64),
        ("loss.validate", "loss-validation-v1", "v" * 64),
    ):
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
        )
        await repository.start_task(
            session,
            run_id,
            task_key,
            algorithm_version,
            fingerprint,
        )
        await repository.complete_task(
            session,
            task.id,
            fingerprint,
            {},
        )


async def _event(kind: EventKind, magnitude: str, report_time: datetime):
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="CORRECTION-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="test",
        report_time=report_time,
    )


async def test_correction_supersedes_old_run_but_keeps_fallback(session_factory) -> None:
    repository = AssessmentRepository()
    first = await _event(
        EventKind.FORMAL,
        "5.2",
        datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    first_outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CORRECTION-1", "type": "reviewed", "number": 1},
        event=first,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            first.magnitude,
            first.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            first_outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == first_outcome.revision_id
                )
            )
            first_run = await repository.ensure_run_from_outbox(
                session,
                event_id=first_outcome.event_id,
                revision_id=first_outcome.revision_id,
                outbox_id=str(first_outbox.id),
            )
            await repository.start_run(session, first_run.id)
            first_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == first_run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            await repository.start_task(
                session,
                first_run.id,
                "intensity.model",
                "model-axis-ratio-v1",
                "f" * 64,
            )
            await repository.complete_task(
                session,
                first_task.id,
                "first-checksum",
                {},
            )
            fusion_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == first_run.id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            await repository.start_task(
                session,
                first_run.id,
                "intensity.fusion",
                "fusion-inverse-variance-v1",
                "g" * 64,
            )
            await repository.complete_task(
                session,
                fusion_task.id,
                "fusion-checksum",
                {},
            )
            await _complete_loss_tasks(session, first_run.id)
            await repository.complete_run(session, first_run.id, "bundle-1")
            first_run.deadline_at = received + timedelta(seconds=1)
            await repository.mark_deadline_exceeded(
                session,
                first_run.id,
                received + timedelta(seconds=2),
            )

    correction = await _event(
        EventKind.CORRECTION,
        "5.4",
        datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
    )
    second_received = datetime(2026, 9, 27, 1, 5, tzinfo=UTC)
    second_outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CORRECTION-1", "type": "reviewed", "number": 2},
        event=correction,
        provider="fan",
        lane="websocket",
        received_at=second_received,
        response_input=ResponseInput(
            correction.magnitude,
            correction.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(True, 0, "grid-test", second_received),
    )

    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == second_outcome.revision_id
                )
            )
            second_run = await repository.ensure_run_from_outbox(
                session,
                event_id=second_outcome.event_id,
                revision_id=second_outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            event = await session.get(EarthquakeEvent, first_run.event_id)
            old = await session.get(AssessmentRun, first_run.id)

    assert second_run.id != first_run.id
    assert old.superseded_by_run_id == second_run.id
    assert old.superseded_at is not None
    assert event.latest_assessment_run_id == second_run.id
    assert event.effective_assessment_run_id == first_run.id
    assert old.deadline_exceeded_at == received + timedelta(seconds=2)
