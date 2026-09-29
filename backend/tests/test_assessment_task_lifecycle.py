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
from app.intensity.domain import GridDefinition
from app.intensity.models import AssessmentTaskAttempt
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
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


async def _ingest(
    session_factory,
    *,
    kind: EventKind,
    magnitude: str,
    report_time: datetime,
    received_at: datetime,
) -> str:
    event = NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="LIFECYCLE-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="test",
        report_time=report_time,
    )
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "LIFECYCLE-1", "type": "reviewed"},
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
            True,
            Decimal("0"),
            "grid-test",
            received_at,
        ),
    )
    return outcome.revision_id


async def _seed_run(session_factory) -> AssessmentRun:
    revision_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == revision_id
                )
            )
            return await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=str(outbox.event_id),
                revision_id=str(outbox.revision_id),
                outbox_id=str(outbox.id),
            )


async def _seed_superseding_run(session_factory) -> AssessmentRun:
    revision_id = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.3",
        report_time=datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
        received_at=datetime(2026, 9, 27, 1, 5, tzinfo=UTC),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == revision_id
                )
            )
            return await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=str(outbox.event_id),
                revision_id=str(outbox.revision_id),
                outbox_id=str(outbox.id),
            )


async def _start_task(
    session_factory,
    run_id,
    task_key: str,
    *,
    algorithm_version: str = "model-axis-ratio-v1",
    fingerprint: str = "f" * 64,
) -> AssessmentTask:
    repository = AssessmentRepository()
    async with session_factory() as session:
        async with session.begin():
            return await repository.start_task(
                session,
                run_id,
                task_key,
                algorithm_version,
                fingerprint,
            )


async def _complete_required_tasks(
    session,
    run_id,
    *,
    exclude: str | None = None,
) -> None:
    repository = AssessmentRepository()
    for task_key, algorithm_version, fingerprint in (
        ("intensity.model", "model-axis-ratio-v1", "a" * 64),
        ("intensity.fusion", "fusion-inverse-variance-v1", "b" * 64),
        ("loss.population", "loss-population-test-v1", "c" * 64),
        ("loss.buildings", "loss-buildings-test-v1", "d" * 64),
        ("loss.casualties", "loss-casualties-test-v1", "e" * 64),
        ("loss.economic", "loss-economic-test-v1", "f" * 64),
        ("loss.resources", "loss-resources-test-v1", "9" * 64),
        ("loss.validate", "loss-validate-test-v1", "0" * 64),
    ):
        if task_key == exclude:
            continue
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
            {"product_id": task_key},
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
                "model-axis-ratio-v1",
                "f" * 64,
            )
            second = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "model-axis-ratio-v1",
                "f" * 64,
            )
            assert first.id == second.id
            assert first.attempt_count == 1
            assert first.algorithm_version == "model-axis-ratio-v1"

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(AssessmentTaskAttempt).where(
                    AssessmentTaskAttempt.task_id == first.id
                )
            )
        ).all()

    assert len(attempts) == 1


async def test_failed_task_retry_creates_new_attempt(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()
    first = await _start_task(session_factory, run.id, "intensity.model")

    async with session_factory() as session:
        async with session.begin():
            await repository.fail_task(session, first.id, "RuntimeError", "boom")

    retried = await _start_task(session_factory, run.id, "intensity.model")

    assert retried.id == first.id
    assert retried.status == "running"
    assert retried.attempt_count == 2

    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(AssessmentTaskAttempt).where(
                    AssessmentTaskAttempt.task_id == retried.id
                )
            )
        ).all()

    assert len(attempts) == 2


async def test_pending_task_cannot_complete_or_fail(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            with pytest.raises(ValueError, match="running"):
                await repository.complete_task(
                    session,
                    task.id,
                    "c" * 64,
                    {},
                )
            with pytest.raises(ValueError, match="running"):
                await repository.fail_task(
                    session,
                    task.id,
                    "RuntimeError",
                    "boom",
                )


async def test_running_task_rejects_changed_key(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()
    task = await _start_task(session_factory, run.id, "intensity.model")

    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match="fingerprint"):
                await repository.start_task(
                    session,
                    run.id,
                    "intensity.model",
                    "model-axis-ratio-v1",
                    "e" * 64,
                )
            with pytest.raises(ValueError, match="algorithm"):
                await repository.start_task(
                    session,
                    run.id,
                    "intensity.model",
                    "model-axis-ratio-v2",
                    "f" * 64,
                )

    refreshed = await _start_task(session_factory, run.id, "intensity.model")
    assert refreshed.id == task.id
    assert refreshed.attempt_count == 1


async def test_succeeded_task_requires_whole_key_for_idempotency(
    session_factory,
) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()
    task = await _start_task(session_factory, run.id, "intensity.model")

    async with session_factory() as session:
        async with session.begin():
            await repository.complete_task(
                session,
                task.id,
                "c" * 64,
                {"product_id": "model"},
            )

    async with session_factory() as session:
        async with session.begin():
            same = await repository.start_task(
                session,
                run.id,
                "intensity.model",
                "model-axis-ratio-v1",
                "f" * 64,
            )
            with pytest.raises(ValueError, match="algorithm"):
                await repository.start_task(
                    session,
                    run.id,
                    "intensity.model",
                    "model-axis-ratio-v2",
                    "f" * 64,
                )
            with pytest.raises(ValueError, match="fingerprint"):
                await repository.start_task(
                    session,
                    run.id,
                    "intensity.model",
                    "model-axis-ratio-v1",
                    "e" * 64,
                )

    assert same.id == task.id
    assert same.status == "succeeded"
    assert same.attempt_count == 1


async def test_terminal_states_cannot_be_overwritten(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()
    task = await _start_task(session_factory, run.id, "intensity.model")

    async with session_factory() as session:
        async with session.begin():
            await repository.fail_task(session, task.id, "RuntimeError", "boom")
            with pytest.raises(ValueError, match="running"):
                await repository.complete_task(
                    session,
                    task.id,
                    "c" * 64,
                    {},
                )
            with pytest.raises(ValueError, match="terminal"):
                await repository.fail_task(
                    session,
                    task.id,
                    "RuntimeError",
                    "second",
                )


async def test_complete_task_is_idempotent_only_for_same_checksum(
    session_factory,
) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()
    task = await _start_task(session_factory, run.id, "intensity.model")

    async with session_factory() as session:
        async with session.begin():
            first = await repository.complete_task(
                session,
                task.id,
                "c" * 64,
                {"product_id": "model"},
            )
            same = await repository.complete_task(
                session,
                task.id,
                "c" * 64,
                {"product_id": "model"},
            )
            with pytest.raises(ValueError, match="checksum"):
                await repository.complete_task(
                    session,
                    task.id,
                    "d" * 64,
                    {"product_id": "model"},
                )

    assert first.status == "succeeded"
    assert same.status == "succeeded"


async def test_completed_run_sets_deadline_and_effective_pointer(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    assert run.deadline_basis_at == datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    assert run.deadline_at == datetime(2026, 9, 27, 1, 8, tzinfo=UTC)
    assert run.started_at is None

    async with session_factory() as session:
        async with session.begin():
            running = await repository.start_run(session, run.id)
            assert running.started_at is not None
            await _complete_required_tasks(session, run.id)
            completed = await repository.complete_run(
                session,
                run.id,
                "bundle-1",
            )
            event = await session.get(EarthquakeEvent, run.event_id)

    assert completed.status == "completed"
    assert event.effective_assessment_run_id == run.id


async def test_complete_run_rejects_missing_required_loss_task(
    session_factory,
) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, run.id)
            await _complete_required_tasks(
                session,
                run.id,
                exclude="loss.population",
            )
            with pytest.raises(ValueError, match="required assessment tasks"):
                await repository.complete_run(session, run.id, "bundle-1")


async def test_run_transition_and_completion_idempotency(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match="running"):
                await repository.complete_run(session, run.id, "bundle-1")
            await repository.start_run(session, run.id)
            await _complete_required_tasks(session, run.id)
            first = await repository.complete_run(session, run.id, "bundle-1")
            same = await repository.complete_run(session, run.id, "bundle-1")
            with pytest.raises(ValueError, match="bundle"):
                await repository.complete_run(session, run.id, "bundle-2")

    assert first.status == "completed"
    assert same.status == "completed"


async def test_fail_run_enforces_running_state(session_factory) -> None:
    run = await _seed_run(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match="running"):
                await repository.fail_run(session, run.id, "boom")
            await repository.start_run(session, run.id)
            failed = await repository.fail_run(session, run.id, "boom")
            with pytest.raises(ValueError, match="terminal"):
                await repository.fail_run(session, run.id, "second")

    assert failed.status == "failed"


async def test_old_run_cannot_overwrite_newer_effective_pointer(
    session_factory,
) -> None:
    first = await _seed_run(session_factory)
    repository = AssessmentRepository()

    second = await _seed_superseding_run(session_factory)
    assert second.id != first.id

    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, first.id)
            await _complete_required_tasks(session, first.id)
            completed = await repository.complete_run(
                session,
                first.id,
                "bundle-1",
            )
            event = await session.get(EarthquakeEvent, first.event_id)

    assert completed.status == "completed"
    assert event.latest_assessment_run_id == second.id
    assert event.effective_assessment_run_id is None


async def test_rollback_surviving_task_failure_audit(session_factory) -> None:
    run = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "audit-grid",
            "EPSG:32651",
            1000,
            0,
            1000,
            1,
            1,
        ),
    )
    error = RuntimeError("working transaction failed")

    with pytest.raises(RuntimeError, match="working transaction failed"):
        async with session_factory() as session:
            async with session.begin():
                await AssessmentRepository().start_task(
                    session,
                    run.id,
                    "intensity.model",
                    "model-axis-ratio-v1",
                    "f" * 64,
                )
                raise error

    with pytest.raises(RuntimeError, match="working transaction failed"):
        await service.record_task_failure(
            str(run.id),
            "intensity.model",
            error,
        )

    async with session_factory() as session:
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run.id,
                AssessmentTask.task_key == "intensity.model",
            )
        )
        attempt = await session.scalar(
            select(AssessmentTaskAttempt).where(
                AssessmentTaskAttempt.task_id == task.id
            )
        )

    assert task.status == "failed"
    assert task.last_error == "RuntimeError: working transaction failed"
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.error_category == "RuntimeError"


async def test_rollback_surviving_run_failure_audit(session_factory) -> None:
    run = await _seed_run(session_factory)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle("/config/intensity/shanghai-2019.yaml"),
        fixed_grid_definition=GridDefinition(
            "run-audit-grid",
            "EPSG:32651",
            1000,
            0,
            1000,
            1,
            1,
        ),
    )
    repository = AssessmentRepository()
    error = RuntimeError("run working transaction failed")

    with pytest.raises(RuntimeError, match="run working transaction failed"):
        async with session_factory() as session:
            async with session.begin():
                await repository.start_run(session, run.id)
                raise error

    with pytest.raises(RuntimeError, match="run working transaction failed"):
        await service.record_run_failure(str(run.id), error)

    async with session_factory() as session:
        failed = await session.get(AssessmentRun, run.id)

    assert failed.status == "failed"
    assert failed.last_error == "RuntimeError: run working transaction failed"
