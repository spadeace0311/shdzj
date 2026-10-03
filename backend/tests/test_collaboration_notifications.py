from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import User
from app.collaboration.domain import DutyRole
from app.collaboration.models import (
    NotificationDelivery,
    WorkgroupMembership,
    WorkgroupTask,
)
from app.collaboration.notifications import (
    AdapterResult,
    DeadlineScheduler,
    NotificationService,
)
from app.collaboration.roster import MemberInput, RosterService
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage


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
        event_type: str = "formal",
        lifecycle_state: str = "formal_triggered",
        origin_time: datetime | None = None,
    ) -> EarthquakeEvent:
        event = EarthquakeEvent(
            id=uuid.uuid4(),
            source="notification-test",
            canonical_source_id=f"notification-{uuid.uuid4().hex}",
            event_type=event_type,
            origin_time=origin_time or datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="notification fixture",
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
            source="notification-test",
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
            is_current=True,
            ingested_at=datetime(2026, 10, 3, 0, 1, tzinfo=UTC),
        )
        session.add(revision)
        await session.flush()
        return revision

    return factory


@pytest.fixture
def leader_factory(session, event_factory):
    async def factory(
        event: EarthquakeEvent,
        *,
        workgroup_code: str = "monitoring_forecast",
    ) -> User:
        leader = User(
            id=uuid.uuid4(),
            username=f"notification-leader-{uuid.uuid4().hex[:8]}",
            password_hash="not-used",
            role="group_leader",
            workgroup=workgroup_code,
            is_active=True,
        )
        session.add(leader)
        await session.flush()
        service = RosterService()
        await service.replace_group_members(
            session,
            workgroup_code,
            [MemberInput(leader.id, DutyRole.LEADER)],
            actor="system",
        )
        await service.snapshot_for_event(session, event.id)
        await service.set_attendance(
            session,
            event.id,
            workgroup_code,
            leader.id,
            "present",
            "system",
        )
        return leader

    return factory


@pytest.fixture
def task_factory(session, event_factory, revision_factory, leader_factory):
    async def factory(
        *,
        event: EarthquakeEvent | None = None,
        status: str = "pending",
        due_at: datetime | None = None,
        workgroup_code: str = "monitoring_forecast",
    ) -> tuple[WorkgroupTask, User]:
        if event is None:
            event = await event_factory()
        revision = await revision_factory(event)
        leader = await leader_factory(event, workgroup_code=workgroup_code)
        now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
        task = WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            task_code=f"notification-{uuid.uuid4().hex}",
            source_type="ad_hoc",
            workgroup_code=workgroup_code,
            title="Notification fixture task",
            instruction="Complete the task.",
            priority=100,
            status=status,
            timeliness_state="on_time",
            phase_code="within_30m",
            activated_at=now,
            due_at=due_at or now + timedelta(hours=1),
            row_version=1,
            created_by="system",
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        await session.flush()
        return task, leader

    return factory


async def _seed_notification_task(
    session,
    *,
    workgroup_code: str = "monitoring_forecast",
) -> tuple[WorkgroupTask, User, EarthquakeEvent]:
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source="notification-concurrency-test",
        canonical_source_id=f"notification-concurrency-{uuid.uuid4().hex}",
        event_type="formal",
        origin_time=datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="notification concurrency fixture",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="formal_triggered",
    )
    session.add(event)
    await session.flush()
    raw = RawMessage(
        id=uuid.uuid4(),
        source="notification-concurrency-test",
        source_message_id=uuid.uuid4().hex,
        message_kind="formal",
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
        revision_kind="formal",
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        is_current=True,
        ingested_at=datetime(2026, 10, 3, 0, 1, tzinfo=UTC),
    )
    session.add(revision)
    await session.flush()
    leader = User(
        id=uuid.uuid4(),
        username=f"notification-concurrency-{uuid.uuid4().hex[:8]}",
        password_hash="not-used",
        role="group_leader",
        workgroup=workgroup_code,
        is_active=True,
    )
    session.add(leader)
    await session.flush()
    service = RosterService()
    await service.replace_group_members(
        session,
        workgroup_code,
        [MemberInput(leader.id, DutyRole.LEADER)],
        actor="system",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session,
        event.id,
        workgroup_code,
        leader.id,
        "present",
        "system",
    )
    task = WorkgroupTask(
        event_id=event.id,
        trigger_revision_id=revision.id,
        task_code=f"notification-concurrency-{uuid.uuid4().hex}",
        source_type="ad_hoc",
        workgroup_code=workgroup_code,
        title="Notification concurrency fixture",
        instruction="Complete the task.",
        priority=100,
        status="pending",
        timeliness_state="on_time",
        phase_code="within_30m",
        activated_at=datetime(2026, 10, 3, 1, 0, tzinfo=UTC),
        due_at=datetime(2026, 10, 3, 2, 0, tzinfo=UTC),
        row_version=1,
        created_by="system",
        created_at=datetime(2026, 10, 3, 1, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 3, 1, 0, tzinfo=UTC),
    )
    session.add(task)
    await session.flush()
    return task, leader, event


async def _deliveries_for(session, task_id: uuid.UUID) -> list[NotificationDelivery]:
    rows = await session.scalars(
        select(NotificationDelivery).where(
            NotificationDelivery.task_id == task_id
        )
    )
    return list(rows)


class FailingAdapter:
    def __init__(self, *, retryable: bool = True) -> None:
        self.calls = 0
        self.retryable = retryable

    async def send(self, delivery: NotificationDelivery) -> AdapterResult:
        del delivery
        self.calls += 1
        return AdapterResult(
            sent=False,
            retryable=self.retryable,
            error="injected failure",
        )


class RecordingInAppAdapter:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def send(self, delivery: NotificationDelivery) -> AdapterResult:
        self.calls.append(delivery.id)
        return AdapterResult(sent=True)


async def test_assignment_due_soon_and_due_deliveries_are_scheduled_once(
    session,
    task_factory,
):
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    task, _leader = await task_factory(due_at=now + timedelta(minutes=10))
    scheduler = DeadlineScheduler(channels=("in_app", "wecom"))

    first = await scheduler.run_once(session, observed_at=now)
    second = await scheduler.run_once(session, observed_at=now)

    assert first.assigned_created == 1
    assert first.due_soon_created == 1
    assert second.assigned_created == 0
    assert second.due_soon_created == 0
    await session.refresh(task)
    assert task.timeliness_state == "at_risk"
    rows = await _deliveries_for(session, task.id)
    assert {row.intent_type for row in rows} >= {"assigned", "due_soon"}
    assert {row.channel for row in rows} >= {"in_app", "wecom"}


async def test_overdue_task_emits_initial_and_thirty_minute_reminders(
    session,
    task_factory,
):
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    task, _leader = await task_factory(due_at=now)
    scheduler = DeadlineScheduler(channels=("in_app",))

    first = await scheduler.run_once(session, observed_at=task.due_at + timedelta(seconds=1))
    second = await scheduler.run_once(
        session,
        observed_at=task.due_at + timedelta(minutes=30),
    )

    assert first.overdue_marked == 1
    assert second.overdue_reminder_count == 1
    rows = await _deliveries_for(session, task.id)
    assert {row.intent_type for row in rows} >= {"overdue"}
    assert len([row for row in rows if row.intent_type == "overdue"]) == 2


async def test_overdue_reminder_buckets_do_not_duplicate_across_reruns(
    session,
    task_factory,
):
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    task, _leader = await task_factory(due_at=now)
    scheduler = DeadlineScheduler(channels=("in_app",))
    observed_at = task.due_at + timedelta(minutes=30)

    first = await scheduler.run_once(session, observed_at=observed_at)
    second = await scheduler.run_once(session, observed_at=observed_at)

    assert first.overdue_reminder_count == 1
    assert second.overdue_reminder_count == 0
    rows = await _deliveries_for(session, task.id)
    assert len([row for row in rows if row.intent_type == "overdue"]) == 2


async def test_external_notification_failure_retries_then_falls_back_to_in_app(
    session,
    event_factory,
    revision_factory,
    leader_factory,
):
    event = await event_factory()
    leader = await leader_factory(event)
    delivery = NotificationDelivery(
        event_id=event.id,
        task_id=None,
        recipient_user_id=leader.id,
        intent_type="assigned",
        channel="wecom",
        dedupe_key="assigned:event-only",
        status="pending",
        attempt_count=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(delivery)
    await session.flush()
    failing = FailingAdapter()
    in_app = RecordingInAppAdapter()
    service = NotificationService(
        adapters={"wecom": failing, "in_app": in_app},
        max_attempts=3,
    )

    first = await service.dispatch_pending(session, limit=10)
    second = await service.dispatch_pending(session, limit=10)
    third = await service.dispatch_pending(session, limit=10)

    assert first.processed == 1
    assert second.processed == 1
    assert third.fallback_sent == 1
    assert failing.calls == 3
    await session.refresh(delivery)
    assert delivery.status == "fallback_sent"
    assert delivery.attempt_count == 3
    fallback_row = await session.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.event_id == event.id,
            NotificationDelivery.task_id.is_(None),
            NotificationDelivery.recipient_user_id == leader.id,
            NotificationDelivery.channel == "in_app",
            NotificationDelivery.dedupe_key == "assigned:event-only",
        )
    )
    assert fallback_row is not None


async def test_scheduler_creates_in_app_before_external_channels(
    session,
    task_factory,
):
    now = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    task, _leader = await task_factory(due_at=now + timedelta(minutes=10))

    await DeadlineScheduler(
        channels=("in_app", "wecom", "email", "phone")
    ).run_once(session, observed_at=now)

    rows = await _deliveries_for(session, task.id)
    assert {"in_app", "wecom", "email", "phone"} <= {
        row.channel for row in rows
    }
    assert rows[0].channel == "in_app"


async def test_test_drill_external_channels_are_suppressed_during_active_formal(
    session,
    event_factory,
    task_factory,
):
    formal = await event_factory(event_type="formal", lifecycle_state="formal_triggered")
    test_event = await event_factory(event_type="test", lifecycle_state="not_applicable")
    drill_event = await event_factory(event_type="drill", lifecycle_state="not_applicable")
    test_task, _test_leader = await task_factory(event=test_event)
    drill_task, _drill_leader = await task_factory(event=drill_event)

    await DeadlineScheduler(
        channels=("in_app", "wecom", "email", "phone")
    ).run_once(session, observed_at=datetime(2026, 10, 3, 1, 0, tzinfo=UTC))

    for task in (test_task, drill_task):
        rows = await _deliveries_for(session, task.id)
        assert {row.channel for row in rows} == {"in_app"}

    assert await session.get(EarthquakeEvent, formal.id) is not None


async def test_dispatch_suppresses_pending_test_external_when_formal_becomes_active(
    session,
    event_factory,
    task_factory,
):
    formal = await event_factory(
        event_type="formal",
        lifecycle_state="not_applicable",
    )
    test_event = await event_factory(
        event_type="test",
        lifecycle_state="not_applicable",
    )
    task, leader = await task_factory(event=test_event)
    delivery = NotificationDelivery(
        event_id=test_event.id,
        task_id=task.id,
        recipient_user_id=leader.id,
        intent_type="assigned",
        channel="wecom",
        dedupe_key="assigned",
        status="pending",
        attempt_count=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(delivery)
    await session.flush()

    formal.lifecycle_state = "formal_triggered"
    await session.flush()
    wecom_adapter = RecordingInAppAdapter()
    in_app_adapter = RecordingInAppAdapter()
    result = await NotificationService(
        adapters={
            "wecom": wecom_adapter,
            "in_app": in_app_adapter,
        }
    ).dispatch_pending(session, limit=10)

    assert result.fallback_sent == 1
    assert wecom_adapter.calls == []
    await session.refresh(delivery)
    assert delivery.status == "fallback_sent"
    fallback_row = await session.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.event_id == test_event.id,
            NotificationDelivery.task_id == task.id,
            NotificationDelivery.recipient_user_id == leader.id,
            NotificationDelivery.channel == "in_app",
            NotificationDelivery.dedupe_key == "assigned",
        )
    )
    assert fallback_row is not None


async def test_external_channels_are_not_suppressed_without_active_formal(
    session,
    event_factory,
    task_factory,
):
    await session.execute(
        update(EarthquakeEvent)
        .where(
            EarthquakeEvent.event_type == "formal",
            EarthquakeEvent.lifecycle_state.in_(
                {
                    "active",
                    "formal_triggered",
                    "correction_triggered",
                    "assessment_triggered",
                }
            ),
        )
        .values(lifecycle_state="not_applicable")
    )
    event = await event_factory(event_type="test", lifecycle_state="not_applicable")
    task, _leader = await task_factory(event=event)

    await DeadlineScheduler(
        channels=("in_app", "wecom", "email", "phone")
    ).run_once(session, observed_at=datetime(2026, 10, 3, 1, 0, tzinfo=UTC))

    rows = await _deliveries_for(session, task.id)
    assert {"in_app", "wecom", "email", "phone"} <= {
        row.channel for row in rows
    }


async def test_concurrent_scheduler_runs_do_not_duplicate_deliveries(
    isolated_session_factory,
):
    async with isolated_session_factory() as seed_session:
        async with seed_session.begin():
            task, leader, event = await _seed_notification_task(seed_session)
            task_id = task.id
            leader_id = leader.id
            event_id = event.id
            raw_id = await seed_session.scalar(
                select(EarthquakeRevision.raw_message_id).where(
                    EarthquakeRevision.event_id == event_id
                )
            )

    observed_at = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)
    scheduler = DeadlineScheduler(channels=("in_app", "wecom"))

    async def insert_one():
        async with isolated_session_factory() as current_session:
            async with current_session.begin():
                loaded_task = await current_session.get(WorkgroupTask, task_id)
                assert loaded_task is not None
                return await scheduler._ensure_deliveries(
                    current_session,
                    loaded_task,
                    leader_id,
                    intent_type="assigned",
                    dedupe_key="assigned",
                    observed_at=observed_at,
                    allowed_channels=("in_app",),
                    existing=set(),
                )

    results = await asyncio.gather(insert_one(), insert_one())

    assert sorted(results) == [0, 1]
    async with isolated_session_factory() as verify_session:
        rows = (
            await verify_session.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.task_id == task_id,
                    NotificationDelivery.intent_type == "assigned",
                )
            )
        ).all()
        assert len(rows) == 1

    async with isolated_session_factory() as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(
                delete(WorkgroupMembership).where(
                    WorkgroupMembership.user_id == leader_id
                )
            )
            await cleanup_session.execute(
                delete(User).where(User.id == leader_id)
            )
            await cleanup_session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
            )
            if raw_id is not None:
                await cleanup_session.execute(
                    delete(RawMessage).where(RawMessage.id == raw_id)
                )
