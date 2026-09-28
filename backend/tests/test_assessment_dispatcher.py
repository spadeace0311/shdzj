import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from temporalio.common import WorkflowIDConflictPolicy

from app.assessment.dispatcher import AssessmentDispatcher
from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.temporal import AssessmentWorkflow, AssessmentWorkflowInput
from app.assessment.worker import (
    TemporalAssessmentStarter,
    build_dispatcher,
    build_worker,
    run_dispatcher,
)
from app.config import Settings
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_dispatcher_data(session_factory):
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


class RecordingStarter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error = error

    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None:
        self.calls.append((workflow_id, payload))
        if self.error is not None:
            raise self.error


class BlockingStarter(RecordingStarter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None:
        self.calls.append((workflow_id, payload))
        self.started.set()
        await self.release.wait()


class BlockingFailureStarter(BlockingStarter):
    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None:
        self.calls.append((workflow_id, payload))
        self.started.set()
        await self.release.wait()
        raise RuntimeError("stale worker failed")


class RecordingTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def start_workflow(
        self,
        workflow: object,
        arg: object,
        *,
        id: str,
        task_queue: str,
        execution_timeout: object,
        id_conflict_policy: object,
    ) -> None:
        self.calls.append(
            {
                "workflow": workflow,
                "arg": arg,
                "id": id,
                "task_queue": task_queue,
                "execution_timeout": execution_timeout,
                "id_conflict_policy": id_conflict_policy,
            }
        )


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch_once(self) -> int:
        self.calls += 1
        return 0


class RecordingWorker:
    def __init__(
        self,
        client: object,
        *,
        task_queue: str,
        workflows: list[object],
        activities: list[object],
        graceful_shutdown_timeout: object,
    ) -> None:
        self.client = client
        self.task_queue = task_queue
        self.workflows = workflows
        self.activities = activities
        self.graceful_shutdown_timeout = graceful_shutdown_timeout


def _configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://earthquake:earthquake@localhost:5432/earthquake",
        jwt_secret="test-jwt-secret-at-least-16-characters",
        superadmin_initial_password="test-superadmin-password-at-least-16-characters",
        temporal_address="temporal-test:7233",
        temporal_namespace="test-namespace",
        temporal_task_queue="assessment-test",
        assessment_dispatcher_enabled=True,
        assessment_outbox_poll_seconds=0.25,
        assessment_outbox_batch_size=7,
        assessment_outbox_max_attempts=4,
        assessment_outbox_lease_seconds=12,
    )


def test_dispatcher_process_factory_uses_configured_limits(session_factory) -> None:
    configured = _configured_settings()
    dispatcher = build_dispatcher(
        client=RecordingTemporalClient(),
        session_factory=session_factory,
        configured=configured,
    )

    assert dispatcher.batch_size == configured.assessment_outbox_batch_size
    assert dispatcher.max_attempts == configured.assessment_outbox_max_attempts


def test_worker_process_factory_uses_configured_task_queue(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _configured_settings()
    monkeypatch.setattr("app.assessment.worker.Worker", RecordingWorker)
    worker = build_worker(
        client=RecordingTemporalClient(),
        session_factory=session_factory,
        configured=configured,
    )

    assert worker.task_queue == configured.temporal_task_queue


async def test_temporal_starter_maps_outbox_payload_to_workflow_input() -> None:
    client = RecordingTemporalClient()
    starter = TemporalAssessmentStarter(
        client,
        task_queue="assessment-test",
        execution_timeout=timedelta(seconds=180),
    )

    await starter.start_assessment(
        workflow_id="assessment:event-1:revision-1",
        payload={
            "event_id": "event-1",
            "revision_id": "revision-1",
            "outbox_id": "outbox-1",
        },
    )

    assert client.calls == [
        {
            "workflow": AssessmentWorkflow.run,
            "arg": AssessmentWorkflowInput(
                event_id="event-1",
                revision_id="revision-1",
                outbox_id="outbox-1",
            ),
            "id": "assessment:event-1:revision-1",
            "task_queue": "assessment-test",
            "execution_timeout": timedelta(seconds=180),
            "id_conflict_policy": WorkflowIDConflictPolicy.USE_EXISTING,
        }
    ]


async def test_run_dispatcher_polls_when_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = _configured_settings()
    dispatcher = RecordingDispatcher()
    sleeps: list[float] = []
    stop_event = asyncio.Event()

    monkeypatch.setattr(
        "app.assessment.worker.build_dispatcher",
        lambda **_kwargs: dispatcher,
    )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        stop_event.set()

    await run_dispatcher(
        stop_event,
        client=RecordingTemporalClient(),
        configured=configured,
        sleep=sleep,
    )

    assert dispatcher.calls == 1
    assert sleeps == [configured.assessment_outbox_poll_seconds]


async def _create_outbox(session_factory) -> EventLifecycleOutbox:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="CENC-DISPATCH-1",
        origin_time=datetime(2026, 9, 26, 2, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 26, 2, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 26, 2, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "CENC-DISPATCH-1", "type": "reviewed"},
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
            boundary_version="test-2026.1",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        outbox = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.revision_id == outcome.revision_id
            )
        )
    assert outbox is not None
    return outbox


async def test_dispatch_once_publishes_pending_outbox(session_factory) -> None:
    outbox = await _create_outbox(session_factory)
    starter = RecordingStarter()
    dispatcher = AssessmentDispatcher(
        session_factory=session_factory,
        starter=starter,
        batch_size=20,
        max_attempts=10,
        lease_seconds=60,
        now=lambda: datetime(2026, 9, 26, 2, 4, tzinfo=UTC),
    )

    dispatched = await dispatcher.dispatch_once()

    async with session_factory() as session:
        persisted = await session.get(EventLifecycleOutbox, outbox.id)

    assert dispatched == 1
    assert persisted is not None
    assert persisted.status == "published"
    assert persisted.attempt_count == 1
    assert persisted.published_at == datetime(2026, 9, 26, 2, 4, tzinfo=UTC)
    assert starter.calls == [
        (
            f"assessment:{persisted.event_id}:{persisted.revision_id}",
            {
                **persisted.payload,
                "outbox_id": str(persisted.id),
            },
        )
    ]


async def test_dispatch_failure_retries_then_dead_letters(session_factory) -> None:
    outbox = await _create_outbox(session_factory)
    starter = RecordingStarter(error=RuntimeError("temporal unavailable"))
    clock = {"now": datetime(2026, 9, 26, 2, 4, tzinfo=UTC)}
    dispatcher = AssessmentDispatcher(
        session_factory=session_factory,
        starter=starter,
        batch_size=20,
        max_attempts=2,
        lease_seconds=60,
        now=lambda: clock["now"],
    )

    first = await dispatcher.dispatch_once()
    async with session_factory() as session:
        after_first = await session.get(EventLifecycleOutbox, outbox.id)

    assert first == 0
    assert after_first is not None
    assert after_first.status == "pending"
    assert after_first.attempt_count == 1
    assert after_first.available_at == clock["now"] + timedelta(seconds=1)
    assert "RuntimeError" in (after_first.last_error or "")

    clock["now"] += timedelta(seconds=1)
    second = await dispatcher.dispatch_once()
    async with session_factory() as session:
        after_second = await session.get(EventLifecycleOutbox, outbox.id)

    assert second == 0
    assert after_second is not None
    assert after_second.status == "dead_letter"
    assert after_second.attempt_count == 2
    assert len(starter.calls) == 2


async def test_concurrent_dispatchers_claim_each_outbox_once(session_factory) -> None:
    await _create_outbox(session_factory)
    starter = BlockingStarter()
    first = AssessmentDispatcher(
        session_factory=session_factory,
        starter=starter,
        batch_size=20,
        max_attempts=10,
        lease_seconds=60,
        now=lambda: datetime(2026, 9, 26, 2, 4, tzinfo=UTC),
    )
    second = AssessmentDispatcher(
        session_factory=session_factory,
        starter=starter,
        batch_size=20,
        max_attempts=10,
        lease_seconds=60,
        now=lambda: datetime(2026, 9, 26, 2, 4, tzinfo=UTC),
    )

    first_task = asyncio.create_task(first.dispatch_once())
    await asyncio.wait_for(starter.started.wait(), timeout=1)
    second_result = await second.dispatch_once()
    starter.release.set()
    first_result = await asyncio.wait_for(first_task, timeout=1)

    assert first_result == 1
    assert second_result == 0
    assert len(starter.calls) == 1


async def test_expired_worker_cannot_overwrite_newer_attempt(session_factory) -> None:
    outbox = await _create_outbox(session_factory)
    stale_starter = BlockingFailureStarter()
    clock = {"now": datetime(2026, 9, 26, 2, 4, tzinfo=UTC)}
    stale_dispatcher = AssessmentDispatcher(
        session_factory=session_factory,
        starter=stale_starter,
        batch_size=20,
        max_attempts=10,
        lease_seconds=1,
        now=lambda: clock["now"],
    )
    recovered_starter = RecordingStarter()
    recovered_dispatcher = AssessmentDispatcher(
        session_factory=session_factory,
        starter=recovered_starter,
        batch_size=20,
        max_attempts=10,
        lease_seconds=1,
        now=lambda: clock["now"],
    )

    stale_task = asyncio.create_task(stale_dispatcher.dispatch_once())
    await asyncio.wait_for(stale_starter.started.wait(), timeout=1)
    clock["now"] += timedelta(seconds=2)
    recovered_result = await recovered_dispatcher.dispatch_once()
    stale_starter.release.set()
    stale_result = await asyncio.wait_for(stale_task, timeout=1)

    async with session_factory() as session:
        persisted = await session.get(EventLifecycleOutbox, outbox.id)

    assert recovered_result == 1
    assert stale_result == 0
    assert persisted is not None
    assert persisted.status == "published"
    assert persisted.attempt_count == 2
