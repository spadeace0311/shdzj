from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import process_health
from app.artifacts.models import ProductionRun, ProductionTask
from app.artifacts.storage import ArtifactStore
from app.assessment.models import AssessmentRun
from app.auth.models import User
from app.collaboration.domain import DutyRole
from app.collaboration.models import (
    CollaborationSettings,
    NotificationDelivery,
    WorkgroupTask,
)
from app.collaboration.notifications import DeadlineScheduler
from app.collaboration.roster import MemberInput, RosterService
from app.collaboration.retention import CollaborationRetentionResult
from app.config import settings
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.operations.health import (
    OperationsHealthState,
    get_operations_health_state,
    health_app,
    read_health_payload,
    set_operations_health_state,
)
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository
from app.operations.scheduler import DailyTestScheduler, OperationsSchedulerResult
from app.operations.schemas import ManualTestTriggerRequest
from app.operations.service import OperationsService
from app.operations.worker import (
    OperationsWorkerCycleResult,
    run_operations_cycle,
    run_operations_worker,
)
from app.regions.models import RegionBoundary


OBSERVED_AT = datetime(2026, 10, 11, 2, 30, tzinfo=UTC)
WORKER_REMINDER_SOURCE = "operations-worker-reminder-test"


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self


class _FakeSessionFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


class _FakeScheduler:
    def __init__(self, result: OperationsSchedulerResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def ensure_schedule(self, session: object, observed_at: datetime) -> object:
        del session, observed_at
        self.calls.append("ensure_schedule")
        return object()

    async def ensure_schedule_with_created(
        self,
        session: object,
        observed_at: datetime,
    ) -> tuple[object, bool]:
        del session, observed_at
        self.calls.append("ensure_schedule")
        return object(), self.result.created > 0

    async def dispatch_due(
        self,
        observed_at: datetime,
        trigger: object,
    ) -> OperationsSchedulerResult:
        del observed_at, trigger
        self.calls.append("dispatch_due")
        return self.result


class _FakePriorityGuard:
    def __init__(self, released: int = 1) -> None:
        self.released = released
        self.calls: list[tuple[datetime]] = []

    async def release_deferred_runs(
        self,
        session: object,
        observed_at: datetime,
    ) -> int:
        del session
        self.calls.append((observed_at,))
        return self.released


class _FakeRetentionService:
    def __init__(
        self,
        result: CollaborationRetentionResult | None = None,
    ) -> None:
        self.result = result or CollaborationRetentionResult()
        self.calls: list[tuple[datetime]] = []

    async def purge_due(
        self,
        session: object,
        observed_at: datetime,
    ) -> CollaborationRetentionResult:
        del session
        self.calls.append((observed_at,))
        return self.result


class _FakeHealthState:
    def __init__(self) -> None:
        self.marked_at: datetime | None = None

    def mark_alive(self, now: datetime | None = None) -> None:
        self.marked_at = now


def _scheduler_result(
    *,
    created: int = 0,
    due: int = 0,
    triggered: int = 0,
    deferred: int = 0,
    failed: int = 0,
) -> OperationsSchedulerResult:
    return OperationsSchedulerResult(
        created=created,
        due=due,
        triggered=triggered,
        deferred=deferred,
        failed=failed,
    )


def _configured(*, interval_seconds: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        operations_retention_interval_seconds=interval_seconds,
    )


async def _ensure_worker_operations_environment(session: object) -> None:
    active_boundary = await session.scalar(
        select(RegionBoundary)
        .where(RegionBoundary.is_active.is_(True))
        .limit(1)
    )
    if active_boundary is None:
        session.add(
            RegionBoundary(
                version=f"worker-test-boundary-{uuid.uuid4().hex}",
                name="worker test boundary",
                local_buffer_km=Decimal("50"),
                geom=WKTElement(
                    "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                    "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                    srid=4326,
                ),
                maritime_geom=WKTElement(
                    "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                    "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                    srid=4326,
                ),
                source_uri="test://operations-worker-boundary",
                checksum="a" * 64,
                is_active=True,
            )
        )
    collaboration_settings = await session.scalar(
        select(CollaborationSettings.id).limit(1)
    )
    if collaboration_settings is None:
        session.add(
            CollaborationSettings(
                id=uuid.UUID("00000000-0000-0000-0000-000000000023"),
                intensity_threshold=Decimal("2.0"),
                row_version=1,
            )
        )
    await OperationsRepository().get_settings(session)


async def _cleanup_worker_operations_event(
    session_factory: object,
    event_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run_ids = list(
                await session.scalars(
                    select(OperationsRun.id).where(
                        OperationsRun.event_id == event_id
                    )
                )
            )
            await session.execute(
                delete(OperationsAuditLog).where(
                    OperationsAuditLog.resource_id.in_(
                        [str(run_id) for run_id in run_ids]
                    )
                )
            )
            await session.execute(
                delete(AssessmentRun).where(
                    AssessmentRun.event_id == event_id
                )
            )
            await session.execute(
                delete(ProductionRun).where(
                    ProductionRun.event_id == event_id
                )
            )
            await session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id == event_id
                )
            )
            await session.execute(
                delete(OperationsRun).where(
                    OperationsRun.event_id == event_id
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id == event_id
                )
            )
            await session.execute(
                delete(RawMessage).where(
                    RawMessage.source == "operations"
                )
            )


async def _seed_worker_reminder_context(
    session_factory: object,
    operation_state: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    event_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    task_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with session_factory() as session:
        async with session.begin():
            event = EarthquakeEvent(
                id=event_id,
                source=WORKER_REMINDER_SOURCE,
                canonical_source_id=(
                    f"{WORKER_REMINDER_SOURCE}:{event_id}"
                ),
                event_type="test",
                origin_time=OBSERVED_AT,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="worker reminder event",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="not_applicable",
            )
            session.add(event)
            await session.flush()

            raw = RawMessage(
                id=uuid.uuid4(),
                source=WORKER_REMINDER_SOURCE,
                source_message_id=f"worker-reminder:{event_id}",
                message_kind="test",
                checksum=uuid.uuid4().hex,
                payload={"event_id": str(event_id)},
            )
            session.add(raw)
            await session.flush()

            revision = EarthquakeRevision(
                id=revision_id,
                event_id=event_id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="test",
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                is_current=True,
                ingested_at=OBSERVED_AT,
            )
            session.add(revision)
            await session.flush()
            event.current_revision_id = revision.id

            run = OperationsRun(
                event_id=event_id,
                trigger_mode="manual_test",
                operation_state=operation_state,
                priority=100,
                idempotency_key=f"worker-reminder:{uuid.uuid4()}",
                triggered_by="worker-test",
                resume_required=False,
                detail={},
                started_at=OBSERVED_AT,
                completed_at=(
                    OBSERVED_AT if operation_state == "failed" else None
                ),
                canceled_at=(
                    OBSERVED_AT if operation_state == "canceled" else None
                ),
            )
            session.add(run)

            user = User(
                id=user_id,
                username=f"worker-leader-{uuid.uuid4().hex[:8]}",
                password_hash="not-used",
                role="group_leader",
                workgroup="monitoring_forecast",
                is_active=True,
            )
            session.add(user)
            await session.flush()

            roster = RosterService()
            await roster.replace_group_members(
                session,
                "monitoring_forecast",
                [MemberInput(user.id, DutyRole.LEADER)],
                actor=WORKER_REMINDER_SOURCE,
            )
            await roster.snapshot_for_event(session, event_id)
            await roster.set_attendance(
                session,
                event_id,
                "monitoring_forecast",
                user_id,
                "present",
                WORKER_REMINDER_SOURCE,
            )

            session.add(
                WorkgroupTask(
                    id=task_id,
                    event_id=event_id,
                    trigger_revision_id=revision_id,
                    task_code=f"worker-reminder-{uuid.uuid4().hex}",
                    source_type="ad_hoc",
                    workgroup_code="monitoring_forecast",
                    title="worker reminder task",
                    instruction="complete work",
                    priority=100,
                    status="pending",
                    timeliness_state="on_time",
                    activated_at=OBSERVED_AT,
                    due_at=OBSERVED_AT + timedelta(minutes=10),
                    row_version=1,
                    created_by=WORKER_REMINDER_SOURCE,
                    created_at=OBSERVED_AT,
                    updated_at=OBSERVED_AT,
                )
            )

    return event_id, task_id, user_id


async def _cleanup_worker_reminder_context(
    session_factory: object,
    event_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.task_id == task_id
                )
            )
            await session.execute(
                delete(WorkgroupTask).where(
                    WorkgroupTask.id == task_id
                )
            )
            await session.execute(
                delete(OperationsRun).where(
                    OperationsRun.event_id == event_id
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id == event_id
                )
            )
            await session.execute(
                delete(RawMessage).where(
                    RawMessage.source == WORKER_REMINDER_SOURCE
                )
            )
            await session.execute(
                delete(User).where(User.id == user_id)
            )


async def test_worker_reconciliation_keeps_missing_production_running(
    session_factory,
) -> None:
    await session_factory.kw["bind"].dispose()
    operations_service = OperationsService(
        session_factory=session_factory,
        now=lambda: OBSERVED_AT,
    )
    async with session_factory() as session:
        async with session.begin():
            await _ensure_worker_operations_environment(session)

    triggered = await operations_service.trigger_manual_test(
        ManualTestTriggerRequest(
            idempotency_key=f"worker-reconcile-{uuid.uuid4()}"
        ),
        "worker-test",
    )
    assert triggered.event_id is not None
    assert triggered.run_id is not None

    try:
        async with session_factory() as session:
            async with session.begin():
                event = await session.get(
                    EarthquakeEvent,
                    triggered.event_id,
                )
                outbox = await session.scalar(
                    select(EventLifecycleOutbox)
                    .where(
                        EventLifecycleOutbox.event_id == triggered.event_id,
                        EventLifecycleOutbox.trigger_type
                        == "assessment.requested",
                    )
                    .limit(1)
                )
                assert event is not None and outbox is not None
                session.add(
                    AssessmentRun(
                        id=uuid.uuid4(),
                        event_id=event.id,
                        revision_id=uuid.UUID(triggered.revision_id),
                        outbox_id=outbox.id,
                        run_no=1,
                        status="completed",
                        priority=100,
                        deadline_at=OBSERVED_AT + timedelta(minutes=5),
                        snapshot={},
                        report_ingested_at=OBSERVED_AT,
                        deadline_basis_at=OBSERVED_AT,
                    )
                )

        cycle_result = await run_operations_cycle(
            session_factory=session_factory,
            observed_at=OBSERVED_AT,
            configured=_configured(interval_seconds=60),
            scheduler=_FakeScheduler(_scheduler_result()),
            operations_service=operations_service,
            priority_guard=_FakePriorityGuard(),
            retention_service=_FakeRetentionService(),
            health_state=_FakeHealthState(),
            last_retention_at=OBSERVED_AT,
        )

        async with session_factory() as session:
            run = await session.get(OperationsRun, triggered.run_id)
            audit = await session.scalar(
                select(OperationsAuditLog)
                .where(
                    OperationsAuditLog.resource_type == "operations_run",
                    OperationsAuditLog.resource_id == str(triggered.run_id),
                    OperationsAuditLog.action == "operations.run.running",
                )
                .order_by(
                    OperationsAuditLog.created_at.desc(),
                    OperationsAuditLog.id.desc(),
                )
                .limit(1)
            )

        assert cycle_result.runs_reconciled == 1
        assert run is not None
        assert run.operation_state == "running"
        assert run.completed_at is None
        assert audit is not None
        assert audit.detail["reason"] == "production_pending"
    finally:
        await _cleanup_worker_operations_event(
            session_factory,
            triggered.event_id,
        )
        await session_factory.kw["bind"].dispose()


@pytest.mark.parametrize("operation_state", ["failed", "canceled"])
async def test_frozen_operations_runs_suppress_worker_deadline_reminders(
    session_factory,
    operation_state: str,
) -> None:
    await session_factory.kw["bind"].dispose()
    event_id, task_id, user_id = await _seed_worker_reminder_context(
        session_factory,
        operation_state,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                result = await DeadlineScheduler(
                    channels=("in_app", "wecom")
                ).run_once(session, observed_at=OBSERVED_AT)
                deliveries = (
                    await session.scalars(
                        select(NotificationDelivery).where(
                            NotificationDelivery.task_id == task_id
                        )
                    )
                ).all()

        assert result.tasks_scanned == 0
        assert result.assigned_created == 0
        assert result.due_soon_created == 0
        assert result.overdue_reminder_count == 0
        assert deliveries == []
        assert user_id is not None
    finally:
        await _cleanup_worker_reminder_context(
            session_factory,
            event_id,
            task_id,
            user_id,
        )
        await session_factory.kw["bind"].dispose()


async def test_cycle_ensures_before_dispatch_and_aggregates_counts() -> None:
    scheduler = _FakeScheduler(
        _scheduler_result(
            created=1,
            due=1,
            triggered=1,
            deferred=2,
            failed=1,
        )
    )
    priority_guard = _FakePriorityGuard()
    health = _FakeHealthState()

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=scheduler,
        priority_guard=priority_guard,
        retention_service=_FakeRetentionService(),
        health_state=health,
        last_retention_at=OBSERVED_AT,
    )

    assert scheduler.calls == ["ensure_schedule", "dispatch_due"]
    assert priority_guard.calls == [(OBSERVED_AT,)]
    assert result.schedules_created == 1
    assert result.schedules_due == 1
    assert result.tests_triggered == 1
    assert result.tests_deferred == 2
    assert result.schedules_failed == 1
    assert result.retention_ran is False
    assert result.retention_test_deleted == 0
    assert result.retention_drill_deleted == 0
    assert health.marked_at == OBSERVED_AT


async def test_cycle_runs_retention_after_interval() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=3, drill_deleted=2)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(_scheduler_result()),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=61),
    )

    assert retention.calls == [(OBSERVED_AT,)]
    assert result.retention_ran is True
    assert result.retention_test_deleted == 3
    assert result.retention_drill_deleted == 2


async def test_cycle_skips_retention_before_interval() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=1)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(_scheduler_result()),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=59),
    )

    assert retention.calls == []
    assert result.retention_ran is False
    assert result.retention_test_deleted == 0


async def test_schedule_failure_does_not_block_retention() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=1)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(
            _scheduler_result(due=1, failed=1)
        ),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=61),
    )

    assert retention.calls == [(OBSERVED_AT,)]
    assert result.schedules_failed == 1
    assert result.retention_ran is True
    assert result.retention_test_deleted == 1


async def test_real_scheduler_reports_created_only_for_first_cycle() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    observed_at = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
    local_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    scheduler = DailyTestScheduler(session_factory=session_factory)
    operations_service = OperationsService(
        session_factory=session_factory,
        now=lambda: observed_at,
    )

    try:
        first = await run_operations_cycle(
            session_factory=session_factory,
            observed_at=observed_at,
            configured=_configured(),
            scheduler=scheduler,
            operations_service=operations_service,
            priority_guard=_FakePriorityGuard(),
            retention_service=_FakeRetentionService(),
            health_state=_FakeHealthState(),
            last_retention_at=observed_at,
        )
        second = await run_operations_cycle(
            session_factory=session_factory,
            observed_at=observed_at,
            configured=_configured(),
            scheduler=scheduler,
            operations_service=operations_service,
            priority_guard=_FakePriorityGuard(),
            retention_service=_FakeRetentionService(),
            health_state=_FakeHealthState(),
            last_retention_at=observed_at,
        )
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(OperationsTestSchedule).where(
                        OperationsTestSchedule.local_date == local_date
                    )
                )
        await engine.dispose()

    assert first.schedules_created == 1
    assert second.schedules_created == 0


async def test_cycle_uses_integrated_retention_and_cleanup(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    async with session_factory() as session:
        async with session.begin():
            event = await session.get(
                EarthquakeEvent,
                seeded_artifact_assessment.event_id,
            )
            revision = await session.get(
                EarthquakeRevision,
                seeded_artifact_assessment.revision_id,
            )
            assert event is not None and revision is not None
            event.event_type = "test"
            revision.revision_kind = "test"
            assessments = (
                await session.scalars(
                    select(AssessmentRun).where(
                        AssessmentRun.event_id
                        == seeded_artifact_assessment.event_id
                    )
                )
            ).all()
            for assessment in assessments:
                assessment.status = "completed"
            productions = (
                await session.scalars(
                    select(ProductionRun).where(
                        ProductionRun.event_id
                        == seeded_artifact_assessment.event_id
                    )
                )
            ).all()
            for production in productions:
                production.status = "completed"
            production_tasks = (
                await session.scalars(
                    select(ProductionTask).where(
                        ProductionTask.production_run_id.in_(
                            select(ProductionRun.id).where(
                                ProductionRun.event_id
                                == seeded_artifact_assessment.event_id
                            )
                        )
                    )
                )
            ).all()
            for production_task in production_tasks:
                production_task.status = "succeeded"

    store = ArtifactStore(
        tmp_path / "artifacts",
        namespace="operations-integrated-retention",
    )
    result = await run_operations_cycle(
        session_factory=session_factory,
        observed_at=seeded_artifact_assessment.now,
        configured=SimpleNamespace(
            operations_retention_interval_seconds=60,
            artifact_storage_root=str(tmp_path / "artifacts"),
            artifact_storage_namespace=store.storage_namespace,
        ),
        scheduler=_FakeScheduler(_scheduler_result()),
        operations_service=OperationsService(
            session_factory=session_factory,
            now=lambda: seeded_artifact_assessment.now,
        ),
        priority_guard=_FakePriorityGuard(),
        artifact_store=store,
        health_state=_FakeHealthState(),
        last_retention_at=(
            seeded_artifact_assessment.now - timedelta(seconds=61)
        ),
    )

    assert result.artifact_test_deleted == 1
    assert result.retention_test_deleted == 1
    assert result.cleanup_failed_count == 0
    async with session_factory() as session:
        event = await session.get(
            EarthquakeEvent,
            seeded_artifact_assessment.event_id,
        )
    assert event is None
    assert artifact.id is not None


async def test_worker_loop_stops_before_cycle_when_event_is_set() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    cycles: list[OperationsWorkerCycleResult] = []

    async def fake_cycle(**kwargs: object) -> OperationsWorkerCycleResult:
        del kwargs
        cycles.append(OperationsWorkerCycleResult())
        return cycles[-1]

    await run_operations_worker(
        stop_event,
        session_factory=_FakeSessionFactory(),
        configured=SimpleNamespace(
            operations_worker_enabled=True,
            operations_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert cycles == []


async def test_worker_loop_isolates_cycle_exceptions() -> None:
    stop_event = asyncio.Event()
    calls = 0

    async def fake_cycle(**kwargs: object) -> OperationsWorkerCycleResult:
        del kwargs
        nonlocal calls
        calls += 1
        stop_event.set()
        raise RuntimeError("cycle failed")

    await run_operations_worker(
        stop_event,
        session_factory=_FakeSessionFactory(),
        configured=SimpleNamespace(
            operations_worker_enabled=True,
            operations_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert calls == 1


def test_health_state_returns_ok_until_stale_window() -> None:
    now = datetime(2026, 10, 11, 2, 30, tzinfo=UTC)
    state = OperationsHealthState(stale_seconds=30)

    state.mark_alive(now)

    assert state.payload(now)["status"] == "ok"
    assert state.payload(now + timedelta(seconds=31))["status"] == "unavailable"


def test_health_endpoint_uses_operations_state() -> None:
    state = OperationsHealthState(stale_seconds=30)
    state.mark_alive(datetime.now(UTC))
    set_operations_health_state(state)

    try:
        response = TestClient(health_app).get("/health")
    finally:
        set_operations_health_state(OperationsHealthState(stale_seconds=30))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert get_operations_health_state() is not state


def test_health_file_tracks_fresh_and_stale_heartbeat(tmp_path: Path) -> None:
    health_file = tmp_path / "operations-health.json"
    state = OperationsHealthState(
        stale_seconds=30,
        health_file=health_file,
    )
    set_operations_health_state(state)
    state = OperationsHealthState(
        stale_seconds=30,
        health_file=health_file,
    )
    now = datetime(2026, 10, 11, 2, 30, tzinfo=UTC)
    state.mark_alive(now)

    status, heartbeat = read_health_payload(
        health_file,
        now=now + timedelta(seconds=29),
    )
    assert status == "ok"
    assert datetime.fromisoformat(heartbeat) == now

    status, heartbeat = read_health_payload(
        health_file,
        now=now + timedelta(seconds=31),
    )
    assert status == "unavailable"
    assert heartbeat == ""


async def test_repeated_cycle_failures_do_not_refresh_health_file(
    tmp_path: Path,
) -> None:
    health_file = tmp_path / "operations-health.json"
    failures = 0
    stop_event = asyncio.Event()

    async def failing_cycle(**kwargs: object) -> OperationsWorkerCycleResult:
        del kwargs
        nonlocal failures
        failures += 1
        raise RuntimeError("repeated cycle failure")

    async def stop_after_three(_seconds: float) -> None:
        if failures >= 3:
            stop_event.set()

    await run_operations_worker(
        stop_event,
        session_factory=_FakeSessionFactory(),
        configured=SimpleNamespace(
            operations_worker_enabled=True,
            operations_worker_poll_seconds=0.01,
        ),
        sleep=stop_after_three,
        worker_cycle=failing_cycle,
    )

    status, heartbeat = read_health_payload(health_file)
    assert status == "unavailable"
    assert heartbeat == ""
    set_operations_health_state(OperationsHealthState(stale_seconds=30))


def _write_cmdline(path: Path, *parts: str) -> None:
    path.write_bytes(b"\0".join(part.encode() for part in parts) + b"\0")


def test_find_process_matches_operations_worker(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "126"
    process_dir.mkdir(parents=True)
    _write_cmdline(
        process_dir / "cmdline",
        "python",
        "-m",
        "app.operations.worker",
    )

    assert process_health.find_process(
        "operations",
        proc_root=proc_root,
        own_pid=999,
    ) == 126
