from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import User
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.domain import DeliverableRequirementKind, DeliverableSourceKind, DutyRole
from app.collaboration.models import (
    CollaborationSettings,
    CollaborationTaskEvent,
    NotificationDelivery,
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupRosterSnapshot,
    WorkgroupTask,
)
from app.collaboration.notifications import DeadlineScheduler
from app.collaboration.roster import MemberInput, RosterService
from app.collaboration.router import get_roster_session
from app.collaboration.service import CollaborationTaskService, DeliverableService
from app.config import settings
from app.db import SessionFactory, engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.main import app
from app.operations.models import OperationsRun, OperationsSettings, OperationsTestSchedule
from app.operations.priority import (
    OperationsPriorityGuard,
    OperationsWorkBlocked,
)
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary


SOURCE = "operations-priority-test"
OBSERVED_AT = datetime(2040, 1, 15, 4, 0, tzinfo=UTC)
LOCAL_DATE = date(2040, 1, 15)


@pytest.fixture(autouse=True)
async def clean_priority_data():
    await engine.dispose()
    await _cleanup_priority_data()
    yield
    app.dependency_overrides.pop(get_roster_session, None)
    app.dependency_overrides.pop(get_current_user, None)
    await _cleanup_priority_data()
    await engine.dispose()


@pytest.fixture
async def session():
    local_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(local_engine, expire_on_commit=False)
    try:
        async with session_factory() as current:
            transaction = await current.begin()
            try:
                yield current
            finally:
                await transaction.rollback()
    finally:
        await local_engine.dispose()


async def _cleanup_priority_data() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(OperationsRun))
            await session.execute(delete(OperationsTestSchedule))
            await session.execute(delete(OperationsSettings))
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.source == SOURCE)
            )
            await session.execute(delete(RawMessage).where(RawMessage.source == SOURCE))


async def _ensure_active_boundary(session: AsyncSession) -> RegionBoundary:
    active = await session.scalar(
        select(RegionBoundary).where(RegionBoundary.is_active.is_(True)).limit(1)
    )
    if active is not None:
        return active
    boundary = RegionBoundary(
        version=f"priority-test-boundary-{uuid.uuid4().hex}",
        name="priority test boundary",
        local_buffer_km=Decimal("50"),
        geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
            "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        maritime_geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
            "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        source_uri="test://operations-priority-boundary",
        checksum=uuid.uuid4().hex * 2,
        is_active=True,
    )
    session.add(boundary)
    await session.flush()
    return boundary


async def _ensure_collaboration_settings(session: AsyncSession) -> None:
    existing = await session.scalar(select(CollaborationSettings.id).limit(1))
    if existing is not None:
        return
    session.add(
        CollaborationSettings(
            id=uuid.UUID("00000000-0000-0000-0000-000000000018"),
            intensity_threshold=Decimal("2.0"),
            row_version=1,
        )
    )
    await session.flush()


async def _seed_event(
    session: AsyncSession,
    *,
    event_type: str,
    origin_time: datetime = OBSERVED_AT,
) -> tuple[EarthquakeEvent, EarthquakeRevision]:
    event_id = uuid.uuid4()
    event = EarthquakeEvent(
        id=event_id,
        source=SOURCE,
        canonical_source_id=f"{SOURCE}:{event_id}",
        event_type=event_type,
        origin_time=origin_time,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="operations priority fixture",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="not_applicable",
    )
    session.add(event)
    await session.flush()
    raw = RawMessage(
        id=uuid.uuid4(),
        source=SOURCE,
        source_message_id=uuid.uuid4().hex,
        message_kind=event_type,
        checksum=uuid.uuid4().hex,
        payload={"event_id": str(event_id)},
    )
    session.add(raw)
    await session.flush()
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event.id,
        raw_message_id=raw.id,
        revision_no=1,
        revision_kind=event_type,
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        is_current=True,
        ingested_at=origin_time,
    )
    session.add(revision)
    await session.flush()
    event.current_revision_id = revision.id
    await session.flush()
    return event, revision


async def _seed_run(
    session: AsyncSession,
    event: EarthquakeEvent,
    *,
    operation_state: str = "running",
) -> OperationsRun:
    run = OperationsRun(
        event_id=event.id,
        trigger_mode="drill" if event.event_type == "drill" else "scheduled_test",
        operation_state=operation_state,
        priority=10,
        idempotency_key=f"{SOURCE}:run:{uuid.uuid4()}",
        triggered_by=SOURCE,
        detail={},
        started_at=OBSERVED_AT,
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_schedule(
    session: AsyncSession,
    *,
    state: str = "scheduled",
    scheduled_at: datetime = OBSERVED_AT,
    local_date: date = LOCAL_DATE,
) -> OperationsTestSchedule:
    schedule = OperationsTestSchedule(
        local_date=local_date,
        window_start=datetime(2040, 1, 14, 18, 0, tzinfo=UTC),
        window_end=datetime(2040, 1, 14, 20, 0, tzinfo=UTC),
        scheduled_at=scheduled_at,
        state=state,
        random_seed=20400115,
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def _seed_superadmin(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        username=f"priority-admin-{uuid.uuid4().hex[:8]}",
        password_hash="not-used",
        role="superadmin",
        workgroup=None,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_task(
    session: AsyncSession,
    *,
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
    status: str,
) -> WorkgroupTask:
    task = WorkgroupTask(
        event_id=event.id,
        trigger_revision_id=revision.id,
        task_code=f"priority-{uuid.uuid4().hex}",
        source_type="ad_hoc",
        workgroup_code="monitoring_forecast",
        title="priority task",
        instruction="complete work",
        priority=100,
        status=status,
        timeliness_state="on_time",
        activated_at=OBSERVED_AT,
        due_at=OBSERVED_AT + timedelta(minutes=10),
        row_version=1,
        created_by=SOURCE,
        created_at=OBSERVED_AT,
        updated_at=OBSERVED_AT,
    )
    session.add(task)
    await session.flush()
    return task


async def _seed_notification_recipient(
    session: AsyncSession,
    event: EarthquakeEvent,
) -> User:
    user = User(
        id=uuid.uuid4(),
        username=f"priority-leader-{uuid.uuid4().hex[:8]}",
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
        actor=SOURCE,
    )
    await roster.snapshot_for_event(session, event.id)
    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        user.id,
        "present",
        SOURCE,
    )
    return user


async def _ingest_formal(
    *,
    source_event_id: str = "PRIORITY-FORMAL-1",
) -> object:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source=SOURCE,
        source_event_id=source_event_id,
        origin_time=OBSERVED_AT - timedelta(minutes=5),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="priority formal event",
        report_time=OBSERVED_AT,
    )
    return await EventService(SessionFactory).ingest_collected(
        raw_payload={
            "EventID": source_event_id,
            "type": "formal",
            "receivedAt": OBSERVED_AT.isoformat(),
        },
        event=event,
        provider="fan",
        lane="websocket",
        received_at=OBSERVED_AT,
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
            boundary_version="priority-test-boundary",
            computed_at=OBSERVED_AT,
        ),
    )


async def test_collected_formal_ingestion_applies_priority_guard() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            schedule = await _seed_schedule(session)
            test_event, _revision = await _seed_event(
                session,
                event_type="test",
            )
            test_run = await _seed_run(session, test_event)
            schedule_id = schedule.id
            run_id = test_run.id

    await _ingest_formal()

    async with SessionFactory() as session:
        stored_schedule = await session.get(OperationsTestSchedule, schedule_id)
        stored_run = await session.get(OperationsRun, run_id)

    assert stored_schedule is not None
    assert stored_schedule.state == "deferred"
    assert stored_run is not None
    assert stored_run.operation_state == "canceled"
    assert stored_run.detail["reason"] == "real_event_priority"


async def test_manual_ingestion_starts_full_assessment_and_applies_priority_guard() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_active_boundary(session)
            await _ensure_collaboration_settings(session)
            schedule = await _seed_schedule(session)
            drill_event, _revision = await _seed_event(
                session,
                event_type="drill",
                origin_time=OBSERVED_AT - timedelta(hours=1),
            )
            drill_run = await _seed_run(session, drill_event)
            schedule_id = schedule.id
            run_id = drill_run.id

    manual = NormalizedEvent(
        kind=EventKind.MANUAL,
        source=SOURCE,
        source_event_id="MANUAL-PRIORITY-1",
        origin_time=OBSERVED_AT - timedelta(minutes=1),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("2.0"),
        place="manual priority event",
    )
    result = await EventService(SessionFactory).ingest(
        {"EventID": manual.source_event_id, "kind": manual.kind.value},
        manual,
        received_at=OBSERVED_AT,
    )

    async with SessionFactory() as session:
        outbox_rows = (
            await session.scalars(
                select(EventLifecycleOutbox.trigger_type).where(
                    EventLifecycleOutbox.event_id == uuid.UUID(result.event_id)
                )
            )
        ).all()
        snapshots = await session.scalar(
            select(func.count())
            .select_from(WorkgroupRosterSnapshot)
            .where(
                WorkgroupRosterSnapshot.event_id == uuid.UUID(result.event_id)
            )
        )
        stored_schedule = await session.get(OperationsTestSchedule, schedule_id)
        stored_run = await session.get(OperationsRun, run_id)

    assert set(outbox_rows) == {
        "collaboration.requested",
        "assessment.requested",
    }
    assert snapshots is not None and snapshots > 0
    assert stored_schedule is not None
    assert stored_schedule.state == "deferred"
    assert stored_run is not None
    assert stored_run.operation_state == "paused"
    assert stored_run.resume_required is True
    assert stored_run.detail["reason"] == "real_event_priority"


@pytest.mark.parametrize(
    ("policy", "expected_state"),
    [("pause", "paused"), ("terminate", "canceled")],
)
async def test_drill_conflict_policy_controls_running_drill(
    session,
    policy: str,
    expected_state: str,
) -> None:
    real_event, _real_revision = await _seed_event(
        session,
        event_type="manual",
    )
    drill_event, _drill_revision = await _seed_event(
        session,
        event_type="drill",
    )
    run = await _seed_run(session, drill_event)
    session.add(
        OperationsSettings(
            settings_key="default",
            drill_conflict_policy=policy,
        )
    )
    await session.flush()

    result = await OperationsPriorityGuard().apply_real_event_start(
        session,
        event_id=real_event.id,
        observed_at=OBSERVED_AT,
    )

    await session.refresh(run)
    assert result.paused_drill_runs + result.canceled_drill_runs == 1
    assert run.operation_state == expected_state
    assert run.detail["reason"] == "real_event_priority"
    assert run.resume_required is (expected_state == "paused")


async def test_release_deferred_runs_restores_only_due_schedules(session) -> None:
    release_at = OBSERVED_AT + timedelta(days=30)
    await _seed_event(
        session,
        event_type="manual",
        origin_time=release_at - timedelta(days=8),
    )
    due = await _seed_schedule(
        session,
        state="deferred",
        scheduled_at=release_at - timedelta(minutes=1),
        local_date=release_at.date(),
    )
    future = await _seed_schedule(
        session,
        state="deferred",
        scheduled_at=release_at + timedelta(hours=1),
        local_date=release_at.date() + timedelta(days=1),
    )
    drill_event, _revision = await _seed_event(session, event_type="drill")
    run = await _seed_run(session, drill_event, operation_state="paused")
    run.resume_required = True
    await session.flush()

    released = await OperationsPriorityGuard().release_deferred_runs(
        session,
        release_at,
    )

    await session.refresh(due)
    await session.refresh(future)
    await session.refresh(run)
    assert released == 1
    assert due.state == "scheduled"
    assert future.state == "deferred"
    assert run.operation_state == "paused"
    assert run.resume_required is True


async def test_active_real_event_prevents_release(session) -> None:
    await _seed_event(session, event_type="formal")
    schedule = await _seed_schedule(
        session,
        state="deferred",
        scheduled_at=OBSERVED_AT - timedelta(minutes=1),
    )

    released = await OperationsPriorityGuard().release_deferred_runs(
        session,
        OBSERVED_AT,
    )

    await session.refresh(schedule)
    assert released == 0
    assert schedule.state == "deferred"


async def test_frozen_event_blocks_new_task_mutations_and_router_maps_409(
    session,
) -> None:
    event, revision = await _seed_event(session, event_type="test")
    await _seed_run(session, event, operation_state="canceled")
    admin = await _seed_superadmin(session)
    service = CollaborationTaskService(
        priority_guard=OperationsPriorityGuard(),
    )
    submit_task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="in_progress",
    )
    return_task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="pending_review",
    )
    cancel_task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="pending",
    )

    with pytest.raises(OperationsWorkBlocked):
        await service.submit(
            session,
            submit_task.id,
            admin,
            submit_task.row_version,
            result_text="blocked",
        )
    with pytest.raises(OperationsWorkBlocked):
        await service.return_to_work(
            session,
            return_task.id,
            admin,
            return_task.row_version,
            reason="blocked",
        )
    with pytest.raises(OperationsWorkBlocked):
        await service.cancel(
            session,
            cancel_task.id,
            admin,
            cancel_task.row_version,
        )

    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        admin.username,
        admin.role,
        admin.workgroup,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    try:
        response = await client.post(
            f"/api/v1/collaboration/tasks/{submit_task.id}/submit",
            headers={"If-Match": str(submit_task.row_version)},
            json={"result_text": "blocked"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 409
    assert response.json()["detail"] == "operations_work_blocked"


async def test_frozen_event_blocks_upload_and_publish(session) -> None:
    event, revision = await _seed_event(session, event_type="drill")
    await _seed_run(session, event, operation_state="paused")
    admin = await _seed_superadmin(session)
    task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="in_progress",
    )
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="priority.result",
        title="priority result",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    version = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=1,
        source_kind=DeliverableSourceKind.MANUAL,
        text_result={"result": "prepared"},
        created_by=admin.username,
    )
    session.add(version)
    await session.flush()
    service = DeliverableService(priority_guard=OperationsPriorityGuard())

    with pytest.raises(OperationsWorkBlocked):
        await service.add_text_version(
            session,
            deliverable.id,
            admin,
            text_result={"result": "blocked"},
        )
    with pytest.raises(OperationsWorkBlocked):
        await service.publish(
            session,
            deliverable.id,
            admin,
            version_id=version.id,
        )


async def test_idempotent_replay_remains_available_when_event_is_frozen(session) -> None:
    event, revision = await _seed_event(session, event_type="test")
    await _seed_run(session, event, operation_state="canceled")
    admin = await _seed_superadmin(session)
    task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="pending_review",
    )
    session.add(
        CollaborationTaskEvent(
            task_id=task.id,
            event_seq=1,
            event_type="task_submitted",
            actor=admin.username,
            from_status="in_progress",
            to_status="pending_review",
            business_version=task.row_version,
            idempotency_key="priority-replay",
            payload={"actor_id": str(admin.id), "task_code": task.task_code},
            occurred_at=OBSERVED_AT,
        )
    )
    await session.flush()

    replayed = await CollaborationTaskService(
        priority_guard=OperationsPriorityGuard(),
    ).submit(
        session,
        task.id,
        admin,
        task.row_version,
        result_text="already submitted",
        idempotency_key="priority-replay",
    )

    assert replayed.id == task.id
    assert replayed.status == "pending_review"


@pytest.mark.parametrize("run_state", ["paused", "canceled"])
async def test_frozen_event_tasks_do_not_generate_deadline_notifications(
    session,
    run_state: str,
) -> None:
    event, revision = await _seed_event(session, event_type="test")
    await _seed_run(session, event, operation_state=run_state)
    leader = await _seed_notification_recipient(session, event)
    task = await _seed_task(
        session,
        event=event,
        revision=revision,
        status="pending",
    )
    task.due_at = OBSERVED_AT + timedelta(minutes=10)
    await session.flush()

    result = await DeadlineScheduler(
        channels=("in_app", "wecom")
    ).run_once(session, observed_at=OBSERVED_AT)

    assert task.event_id not in result.affected_event_ids
    deliveries = await session.scalar(
        select(func.count())
        .select_from(NotificationDelivery)
        .where(NotificationDelivery.task_id == task.id)
    )
    assert deliveries == 0
    assert leader.id is not None
