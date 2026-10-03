from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import User
from app.collaboration.domain import DutyRole
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    NotificationDelivery,
    WorkgroupMembership,
    WorkgroupTask,
)
from app.collaboration.notifications import DispatchResult
from app.collaboration.roster import MemberInput, RosterService
from app.collaboration.scheduler import DeadlineScheduler, SchedulerResult
from app.collaboration.service import (
    CollaborationTaskService,
    StaleTaskVersion,
    TemporaryTaskService,
)
from app.collaboration.worker import (
    ProjectionOutboxDispatcher,
    WorkerCycleResult,
    run_collaboration_worker,
    run_worker_cycle,
)
from app.config import settings
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)


EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000009")


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def isolated_session_factory():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.fixture
def event_factory(session):
    async def factory(
        *,
        event_id: uuid.UUID = EVENT_ID,
        event_type: str = "formal",
        lifecycle_state: str = "formal_triggered",
        origin_time: datetime | None = None,
    ) -> EarthquakeEvent:
        event = EarthquakeEvent(
            id=event_id,
            source="collaboration-worker-test",
            canonical_source_id=f"worker-{uuid.uuid4().hex}",
            event_type=event_type,
            origin_time=origin_time or datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="worker fixture",
            geom=WKTElement("POINT(121.5 31.2)", srid=4326),
            lifecycle_state=lifecycle_state,
        )
        session.add(event)
        await session.flush()
        return event

    return factory


@pytest.fixture
def revision_factory(session):
    async def factory(event: EarthquakeEvent) -> EarthquakeRevision:
        raw = RawMessage(
            id=uuid.uuid4(),
            source="collaboration-worker-test",
            source_message_id=uuid.uuid4().hex,
            message_kind=event.event_type,
            checksum=uuid.uuid4().hex,
            payload={"event_id": str(event.id)},
        )
        session.add(raw)
        await session.flush()
        revision = EarthquakeRevision(
            id=uuid.uuid4(),
            event_id=event.id,
            raw_message_id=raw.id,
            revision_no=1,
            revision_kind=event.event_type,
            origin_time=event.origin_time,
            longitude=event.longitude,
            latitude=event.latitude,
            depth_km=event.depth_km,
            magnitude=event.magnitude,
            place=event.place,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            is_current=True,
            ingested_at=event.origin_time,
        )
        session.add(revision)
        await session.flush()
        return revision

    return factory


@pytest.fixture
def user_factory(session):
    async def factory(
        label: str,
        role: str,
        workgroup: str | None,
        *,
        membership_group: str | None = None,
        duty_role: str | None = None,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            username=f"worker-{label}-{uuid.uuid4().hex[:8]}",
            password_hash="not-used",
            role=role,
            workgroup=workgroup,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        if membership_group is not None:
            session.add(
                WorkgroupMembership(
                    user_id=user.id,
                    workgroup_code=membership_group,
                    duty_role=duty_role or "member",
                    deputy_order=1 if duty_role == "deputy" else None,
                    is_active=True,
                    created_by="system",
                )
            )
            await session.flush()
        return user

    return factory


@pytest.fixture
def coordination_user(user_factory):
    return lambda: user_factory(
        "coordination",
        "group_member",
        None,
        membership_group="comprehensive_coordination",
        duty_role="member",
    )


@pytest.fixture
def center_station_member(user_factory):
    return lambda: user_factory(
        "center-station",
        "group_member",
        None,
        membership_group="center_station",
        duty_role="member",
    )


async def _create_temporary_task(
    session,
    event,
    creator,
    *,
    workgroup_code: str = "center_station",
    due_at: datetime | None = None,
    continues_until_cancelled: bool = False,
    idempotency_key: str | None = None,
):
    due_at = due_at or datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
    return await TemporaryTaskService().create(
        session,
        event_id=event.id,
        workgroup_code=workgroup_code,
        title="补充检查观测点",
        instruction="检查受影响观测点并上传结果",
        priority=80,
        due_at=due_at,
        continues_until_cancelled=continues_until_cancelled,
        actor=creator,
        idempotency_key=idempotency_key,
    )


async def test_coordination_user_can_create_temporary_task_for_one_group(
    session,
    event_factory,
    revision_factory,
    coordination_user,
):
    event = await event_factory()
    await revision_factory(event)
    task = await TemporaryTaskService().create(
        session,
        event_id=event.id,
        workgroup_code="center_station",
        title="补充检查观测点",
        instruction="检查受影响观测点并上传结果",
        priority=80,
        due_at=datetime(2026, 10, 3, 3, 0, tzinfo=UTC),
        actor=await coordination_user(),
    )

    assert task.source_type == "ad_hoc"
    assert task.template_version_id is None
    assert task.workgroup_code == "center_station"
    assert task.row_version == 1

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [item.workgroup_code for item in tasks] == ["center_station"]

    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
    ).all()
    outbox = (
        await session.scalars(
            select(CollaborationOutbox).where(
                CollaborationOutbox.task_id == task.id
            )
        )
    ).all()
    assert [item.event_type for item in events] == ["task_created"]
    assert events[0].from_status is None
    assert events[0].to_status == "pending"
    assert [item.event_type for item in outbox] == ["task_created"]
    assert outbox[0].status == "pending"


async def test_temporary_task_permission_uses_membership_not_workgroup_string(
    session,
    event_factory,
    revision_factory,
    user_factory,
):
    event = await event_factory()
    await revision_factory(event)
    user = await user_factory(
        "coordination-string",
        "group_member",
        "comprehensive_coordination",
    )

    with pytest.raises(PermissionError):
        await TemporaryTaskService().create(
            session,
            event_id=event.id,
            workgroup_code="center_station",
            title="补充检查观测点",
            instruction="检查受影响观测点并上传结果",
            priority=80,
            due_at=datetime(2026, 10, 3, 3, 0, tzinfo=UTC),
            actor=user,
        )


async def test_temporary_task_requires_due_time_or_continuous_marker(
    session,
    event_factory,
    revision_factory,
    coordination_user,
):
    event = await event_factory()
    await revision_factory(event)
    creator = await coordination_user()
    service = TemporaryTaskService()

    with pytest.raises(ValueError):
        await service.create(
            session,
            event_id=event.id,
            workgroup_code="center_station",
            title="补充检查观测点",
            instruction="检查受影响观测点并上传结果",
            priority=80,
            due_at=None,
            continues_until_cancelled=False,
            actor=creator,
        )

    with pytest.raises(ValueError):
        await service.create(
            session,
            event_id=event.id,
            workgroup_code="center_station",
            title="补充检查观测点",
            instruction="检查受影响观测点并上传结果",
            priority=80,
            due_at=datetime(2026, 10, 3, 3, 0, tzinfo=UTC),
            continues_until_cancelled=True,
            actor=creator,
        )

    continuous = await service.create(
        session,
        event_id=event.id,
        workgroup_code="center_station",
        title="补充检查观测点",
        instruction="检查受影响观测点并上传结果",
        priority=80,
        due_at=None,
        continues_until_cancelled=True,
        actor=creator,
    )
    assert continuous.due_at is None


async def test_temporary_task_creation_is_idempotent(
    session,
    event_factory,
    revision_factory,
    coordination_user,
):
    event = await event_factory()
    await revision_factory(event)
    creator = await coordination_user()
    first = await _create_temporary_task(
        session,
        event,
        creator,
        idempotency_key="temporary-create-once",
    )
    second = await _create_temporary_task(
        session,
        event,
        creator,
        idempotency_key="temporary-create-once",
    )

    assert first.id == second.id
    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == first.id
            )
        )
    ).all()
    outbox = (
        await session.scalars(
            select(CollaborationOutbox).where(
                CollaborationOutbox.task_id == first.id
            )
        )
    ).all()
    assert len(events) == 1
    assert len(outbox) == 1


async def test_temporary_task_uses_shared_lifecycle_and_version(
    session,
    event_factory,
    revision_factory,
    coordination_user,
    center_station_member,
):
    event = await event_factory()
    await revision_factory(event)
    task = await _create_temporary_task(
        session,
        event,
        await coordination_user(),
    )
    member = await center_station_member()

    started = await CollaborationTaskService().start(
        session,
        task.id,
        member,
        task.row_version,
    )
    assert started.status == "in_progress"
    assert started.row_version == 2

    with pytest.raises(StaleTaskVersion):
        await CollaborationTaskService().start(
            session,
            task.id,
            member,
            started.row_version + 1,
        )


async def test_creator_can_update_and_cancel_before_start(
    session,
    event_factory,
    revision_factory,
    coordination_user,
):
    event = await event_factory()
    await revision_factory(event)
    creator = await coordination_user()
    task = await _create_temporary_task(session, event, creator)
    service = TemporaryTaskService()

    updated = await service.update(
        session,
        task.id,
        creator,
        task.row_version,
        title="更新后的临时任务",
        instruction="更新后的任务说明",
        due_at=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
    )
    assert updated.title == "更新后的临时任务"
    assert updated.row_version == 2

    cancelled = await service.cancel(
        session,
        task.id,
        creator,
        updated.row_version,
        reason="no longer needed",
    )
    assert cancelled.status == "not_required"
    assert cancelled.closed_at is not None

    events = (
        await session.scalars(
            select(CollaborationTaskEvent)
            .where(CollaborationTaskEvent.task_id == task.id)
            .order_by(CollaborationTaskEvent.event_seq)
        )
    ).all()
    assert [item.event_type for item in events] == [
        "task_created",
        "task_updated",
        "task_cancelled",
    ]


async def test_temporary_task_assignment_uses_notification_delivery_contract(
    session,
    event_factory,
    revision_factory,
    coordination_user,
    user_factory,
):
    event = await event_factory()
    await revision_factory(event)
    creator = await coordination_user()
    task = await _create_temporary_task(session, event, creator)
    leader = await user_factory(
        "center-leader",
        "group_leader",
        None,
        membership_group="center_station",
        duty_role="leader",
    )
    roster = RosterService()
    await roster.replace_group_members(
        session,
        "center_station",
        [MemberInput(leader.id, DutyRole.LEADER)],
        actor="system",
    )
    await roster.snapshot_for_event(session, event.id)
    await roster.set_attendance(
        session,
        event.id,
        "center_station",
        leader.id,
        "present",
        "system",
    )

    observed_at = task.activated_at + timedelta(seconds=1)
    scheduler_result = await DeadlineScheduler(channels=("in_app",)).run_once(
        session,
        observed_at=observed_at,
    )
    assert scheduler_result.assigned_created == 1

    deliveries = (
        await session.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.task_id == task.id,
                NotificationDelivery.intent_type == "assigned",
            )
        )
    ).all()
    assert [delivery.channel for delivery in deliveries] == ["in_app"]


async def test_post_start_due_or_instruction_change_appends_event(
    session,
    event_factory,
    revision_factory,
    coordination_user,
    center_station_member,
):
    event = await event_factory()
    await revision_factory(event)
    creator = await coordination_user()
    task = await _create_temporary_task(session, event, creator)
    member = await center_station_member()
    started = await CollaborationTaskService().start(
        session,
        task.id,
        member,
        task.row_version,
    )

    updated = await TemporaryTaskService().update(
        session,
        task.id,
        creator,
        started.row_version,
        instruction="更新后的任务说明",
        due_at=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
    )
    assert updated.status == "in_progress"
    assert updated.row_version == 3

    events = (
        await session.scalars(
            select(CollaborationTaskEvent)
            .where(CollaborationTaskEvent.task_id == task.id)
            .order_by(CollaborationTaskEvent.event_seq)
        )
    ).all()
    assert [item.event_type for item in events] == [
        "task_created",
        "task_started",
        "task_updated",
    ]

    with pytest.raises(ValueError):
        await TemporaryTaskService().update(
            session,
            task.id,
            creator,
            updated.row_version,
            title="开始后不能改标题",
        )


async def _seed_worker_outbox(
    session_factory,
    observed_at: datetime,
) -> tuple[uuid.UUID, uuid.UUID]:
    event_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    async with session_factory() as session:
        async with session.begin():
            event = EarthquakeEvent(
                id=event_id,
                source="worker-cycle-test",
                canonical_source_id=f"worker-cycle-{event_id}",
                event_type="formal",
                origin_time=observed_at,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="worker cycle fixture",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="formal_triggered",
            )
            session.add(event)
            await session.flush()
            raw = RawMessage(
                id=raw_id,
                source="worker-cycle-test",
                source_message_id=raw_id.hex,
                message_kind="formal",
                checksum=raw_id.hex,
                payload={"event_id": str(event_id)},
            )
            session.add(raw)
            await session.flush()
            revision = EarthquakeRevision(
                id=uuid.uuid4(),
                event_id=event.id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="formal",
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                inside_shanghai=True,
                distance_to_boundary_km=Decimal("0"),
                is_current=True,
                ingested_at=observed_at,
            )
            session.add(revision)
            await session.flush()
            session.add(
                EventLifecycleOutbox(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    revision_id=revision.id,
                    trigger_type="collaboration.requested",
                    trigger_reason="live",
                    payload={
                        "event_id": str(event.id),
                        "revision_id": str(revision.id),
                        "revision_no": 1,
                    },
                    status="pending",
                    attempt_count=0,
                    created_at=observed_at,
                    available_at=observed_at,
                )
            )
    return event_id, raw_id


async def _cleanup_worker_event(
    session_factory,
    event_id: uuid.UUID,
    raw_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.id == raw_id)
            )


async def test_worker_processes_outbox_and_scheduler_once(
    isolated_session_factory,
):
    observed_at = datetime(2026, 10, 3, 0, 1, tzinfo=UTC)
    event_id, raw_id = await _seed_worker_outbox(
        isolated_session_factory,
        observed_at,
    )

    try:
        result = await run_worker_cycle(
            session_factory=isolated_session_factory,
            observed_at=observed_at,
        )

        assert result.generated_tasks >= 1
        assert result.scheduler_ran is True
        async with isolated_session_factory() as session:
            tasks = (
                await session.scalars(
                    select(WorkgroupTask).where(
                        WorkgroupTask.event_id == event_id
                    )
                )
            ).all()
            outbox_status = await session.scalar(
                select(EventLifecycleOutbox.status).where(
                    EventLifecycleOutbox.event_id == event_id,
                    EventLifecycleOutbox.trigger_type
                    == "collaboration.requested",
                )
            )
        assert len(tasks) >= 1
        assert outbox_status == "published"
    finally:
        await _cleanup_worker_event(
            isolated_session_factory,
            event_id,
            raw_id,
        )


class _FakeDispatcher:
    def __init__(self, label: str, count: int = 1) -> None:
        self.label = label
        self.count = count
        self.calls = 0

    async def dispatch_once(self) -> int:
        self.calls += 1
        return self.count


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self, session, observed_at) -> SchedulerResult:
        del session, observed_at
        self.calls += 1
        return SchedulerResult(
            tasks_scanned=3,
            timeliness_updated=1,
            assigned_created=2,
        )


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch_pending(self, session, limit) -> DispatchResult:
        del session, limit
        self.calls += 1
        return DispatchResult(processed=4, sent=2)


async def test_worker_cycle_calls_services_in_order_and_logs_counts(
    isolated_session_factory,
    caplog,
):
    observed_at = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    order: list[str] = []

    class OrderedCollaborationDispatcher(_FakeDispatcher):
        async def dispatch_once(self) -> int:
            order.append("collaboration")
            return 2

    class OrderedProjectionDispatcher(_FakeDispatcher):
        async def dispatch_once(self) -> int:
            order.append("projection")
            return 3

    class OrderedScheduler(_FakeScheduler):
        async def run_once(self, session, observed_at) -> SchedulerResult:
            order.append("scheduler")
            return await super().run_once(session, observed_at)

    class OrderedNotificationService(_FakeNotificationService):
        async def dispatch_pending(self, session, limit) -> DispatchResult:
            order.append("notification")
            return await super().dispatch_pending(session, limit)

    with caplog.at_level("INFO", logger="app.collaboration.worker"):
        result = await run_worker_cycle(
            session_factory=isolated_session_factory,
            observed_at=observed_at,
            collaboration_dispatcher=OrderedCollaborationDispatcher("collaboration"),
            projection_dispatcher=OrderedProjectionDispatcher("projection"),
            scheduler=OrderedScheduler(),
            notification_service=OrderedNotificationService(),
            notification_limit=10,
        )

    assert order == ["collaboration", "projection", "scheduler", "notification"]
    assert result.generated_tasks == 2
    assert result.projection_dispatched == 3
    assert result.tasks_scanned == 3
    assert result.notifications_processed == 4
    assert '"collaboration":2' in caplog.text
    assert '"projection":3' in caplog.text


async def test_worker_loop_stops_before_cycle_when_event_is_set(
    isolated_session_factory,
):
    stop_event = asyncio.Event()
    stop_event.set()
    cycles: list[WorkerCycleResult] = []

    async def fake_cycle(**kwargs) -> WorkerCycleResult:
        del kwargs
        cycles.append(WorkerCycleResult())
        return cycles[-1]

    await run_collaboration_worker(
        stop_event,
        session_factory=isolated_session_factory,
        configured=SimpleNamespace(
            collaboration_worker_enabled=True,
            collaboration_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert cycles == []


async def test_worker_loop_isolates_cycle_exceptions(
    isolated_session_factory,
):
    stop_event = asyncio.Event()
    calls = 0

    async def fake_cycle(**kwargs) -> WorkerCycleResult:
        del kwargs
        nonlocal calls
        calls += 1
        stop_event.set()
        raise RuntimeError("cycle failed")

    await run_collaboration_worker(
        stop_event,
        session_factory=isolated_session_factory,
        configured=SimpleNamespace(
            collaboration_worker_enabled=True,
            collaboration_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert calls == 1


async def test_projection_dispatcher_skips_without_handler(
    isolated_session_factory,
):
    dispatcher = ProjectionOutboxDispatcher(isolated_session_factory)
    assert await dispatcher.dispatch_once() == 0
