import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import User
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.domain import (
    DeliverableRequirementKind,
    DeliverableSourceKind,
    DutyRole,
    TaskStatus,
)
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupMembership,
    WorkgroupTask,
    WorkgroupTaskContributor,
)
from app.collaboration.roster import MemberInput, RosterService
from app.collaboration.router import get_roster_session
from app.collaboration.service import (
    CollaborationTaskService,
    MissingRequiredDeliverableError,
    StaleTaskVersion,
)
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.main import app


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
def event_factory(session):
    async def factory(*, event_type: str = "formal") -> EarthquakeEvent:
        event_id = uuid.uuid4()
        event = EarthquakeEvent(
            id=event_id,
            source="collaboration-task-test",
            canonical_source_id=f"task-{event_id}",
            event_type=event_type,
            origin_time=datetime.now(UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="collaboration task fixture",
            geom=WKTElement("POINT(121.5 31.2)", srid=4326),
            lifecycle_state="active",
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
            source="collaboration-task-test",
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
            ingested_at=datetime.now(UTC),
        )
        session.add(revision)
        await session.flush()
        return revision

    return factory


@pytest.fixture
def task_factory(session, event_factory, revision_factory):
    async def factory(
        *,
        status: str = "pending",
        workgroup_code: str = "monitoring_forecast",
        event: EarthquakeEvent | None = None,
        revision: EarthquakeRevision | None = None,
        due_at: datetime | None = None,
    ) -> WorkgroupTask:
        if event is None:
            event = await event_factory()
        if revision is None:
            revision = await session.scalar(
                select(EarthquakeRevision)
                .where(EarthquakeRevision.event_id == event.id)
                .order_by(EarthquakeRevision.revision_no)
                .limit(1)
            )
            if revision is None:
                revision = await revision_factory(event)
        now = datetime.now(UTC)
        task = WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            task_code=f"task-{uuid.uuid4().hex}",
            source_type="ad_hoc",
            workgroup_code=workgroup_code,
            title="Task lifecycle fixture",
            instruction="Process the task.",
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
        return task

    return factory


@pytest.fixture
def user_factory(session):
    async def factory(
        label: str,
        role: str,
        workgroup: str | None,
        *,
        duty_role: str | None = None,
        group_code: str | None = None,
        deputy_order: int | None = None,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            username=f"task-{label}-{uuid.uuid4().hex[:8]}",
            password_hash="not-used",
            role=role,
            workgroup=workgroup,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        if duty_role is not None:
            session.add(
                WorkgroupMembership(
                    user_id=user.id,
                    workgroup_code=group_code or workgroup,
                    duty_role=duty_role,
                    deputy_order=(
                        deputy_order
                        if deputy_order is not None
                        else (1 if duty_role == "deputy" else None)
                    ),
                    is_active=True,
                    created_by="system",
                )
            )
            await session.flush()
        return user

    return factory


@pytest.fixture
def group_member_user(user_factory):
    return lambda: user_factory(
        "member",
        "group_member",
        "monitoring_forecast",
        duty_role="member",
        group_code="monitoring_forecast",
    )


@pytest.fixture
def other_group_user(user_factory):
    return lambda: user_factory(
        "other-member",
        "group_member",
        "news_information",
        duty_role="member",
        group_code="news_information",
    )


@pytest.fixture
def group_leader_user(user_factory):
    return lambda: user_factory(
        "leader",
        "group_leader",
        "monitoring_forecast",
        duty_role="leader",
        group_code="monitoring_forecast",
    )


@pytest.fixture
def group_deputy_user(user_factory):
    return lambda: user_factory(
        "deputy",
        "group_deputy",
        "monitoring_forecast",
        duty_role="deputy",
        group_code="monitoring_forecast",
    )


async def _make_leader_authority(
    session,
    event: EarthquakeEvent,
    leader: User,
    member: User | None = None,
) -> None:
    members = [MemberInput(leader.id, DutyRole.LEADER)]
    if member is not None:
        members.append(MemberInput(member.id, DutyRole.MEMBER))
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        members,
        actor="system",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        leader.id,
        "present",
        "system",
    )


async def _make_member_roster(
    session,
    event: EarthquakeEvent,
    member: User,
) -> None:
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(member.id, DutyRole.MEMBER)],
        actor="system",
    )
    await service.snapshot_for_event(session, event.id)


async def test_task_state_machine_and_group_contribution(
    session,
    task_factory,
    group_member_user,
):
    task = await task_factory(status="pending")
    member = await group_member_user()
    service = CollaborationTaskService()
    await _make_member_roster(
        session,
        await _task_event(session, task.id),
        member,
    )

    started = await service.start(
        session,
        task.id,
        actor=member,
        expected_version=task.row_version,
    )
    assert started.status == TaskStatus.IN_PROGRESS

    submitted = await service.submit(
        session,
        task.id,
        actor=member,
        expected_version=started.row_version,
        result_text="已完成现场联系",
    )
    assert submitted.status == TaskStatus.PENDING_REVIEW

    contributors = (
        await session.scalars(
            select(WorkgroupTaskContributor).where(
                WorkgroupTaskContributor.task_id == task.id
            )
        )
    ).all()
    assert [item.user_id for item in contributors] == [member.id]

    events = (
        await session.scalars(
            select(CollaborationTaskEvent)
            .where(CollaborationTaskEvent.task_id == task.id)
            .order_by(CollaborationTaskEvent.event_seq)
        )
    ).all()
    assert [(item.event_type, item.business_version) for item in events] == [
        ("task_started", 2),
        ("task_submitted", 3),
    ]

    outbox = (
        await session.scalars(
            select(CollaborationOutbox)
            .where(CollaborationOutbox.task_id == task.id)
            .order_by(CollaborationOutbox.created_at)
        )
    ).all()
    assert [item.event_type for item in outbox] == [
        "task_started",
        "task_submitted",
    ]


async def test_wrong_group_user_cannot_start_task(
    session,
    task_factory,
    other_group_user,
):
    task = await task_factory(workgroup_code="monitoring_forecast")
    with pytest.raises(PermissionError):
        await CollaborationTaskService().start(
            session,
            task.id,
            actor=await other_group_user(),
            expected_version=task.row_version,
        )


async def test_task_writes_require_current_event_roster_snapshot(
    session,
    task_factory,
    group_member_user,
):
    task = await task_factory(status="pending")
    member = await group_member_user()

    with pytest.raises(PermissionError):
        await CollaborationTaskService().start(
            session,
            task.id,
            actor=member,
            expected_version=task.row_version,
        )


async def test_stale_version_is_rejected(
    session,
    task_factory,
    group_member_user,
):
    task = await task_factory(status="pending")
    with pytest.raises(StaleTaskVersion):
        await CollaborationTaskService().start(
            session,
            task.id,
            actor=await group_member_user(),
            expected_version=task.row_version + 1,
        )


async def test_idempotent_replay_requires_same_actor_and_operation(
    session,
    task_factory,
    group_member_user,
):
    task = await task_factory(status="pending")
    member = await group_member_user()
    other_member = await group_member_user()
    service = CollaborationTaskService()
    await _make_member_roster(
        session,
        await _task_event(session, task.id),
        member,
    )

    started = await service.start(
        session,
        task.id,
        member,
        task.row_version,
        idempotency_key="shared-start",
    )
    assert started.row_version == 2

    with pytest.raises(PermissionError):
        await service.start(
            session,
            task.id,
            other_member,
            started.row_version,
            idempotency_key="shared-start",
        )

    with pytest.raises(ValueError):
        await service.submit(
            session,
            task.id,
            member,
            started.row_version,
            result_text="different operation",
            idempotency_key="shared-start",
        )

    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
    ).all()
    assert len(events) == 1


async def test_return_to_work_and_cancel(
    session,
    task_factory,
    group_member_user,
    group_leader_user,
):
    task = await task_factory(status="pending")
    member = await group_member_user()
    leader = await group_leader_user()
    service = CollaborationTaskService()
    await _make_leader_authority(
        session,
        await _task_event(session, task.id),
        leader,
        member,
    )

    started = await service.start(session, task.id, member, task.row_version)
    submitted = await service.submit(
        session,
        task.id,
        member,
        started.row_version,
        result_text="draft",
    )
    await _make_leader_authority(session, await _task_event(session, task.id), leader)
    returned = await service.return_to_work(
        session,
        task.id,
        leader,
        submitted.row_version,
        reason="please revise",
    )
    assert returned.status == TaskStatus.IN_PROGRESS

    cancelled = await service.cancel(
        session,
        task.id,
        leader,
        returned.row_version,
        reason="no longer needed",
    )
    assert cancelled.status == TaskStatus.NOT_REQUIRED
    assert cancelled.closed_at is not None


async def test_complete_requires_confirming_authority(
    session,
    task_factory,
    group_member_user,
    group_leader_user,
):
    task = await task_factory(status="pending_review")
    member = await group_member_user()
    leader = await group_leader_user()
    service = CollaborationTaskService()
    with pytest.raises(PermissionError):
        await service.complete(
            session,
            task.id,
            member,
            task.row_version,
        )

    await _make_leader_authority(session, await _task_event(session, task.id), leader)
    completed = await service.complete(
        session,
        task.id,
        leader,
        task.row_version,
    )
    assert completed.status == TaskStatus.COMPLETED
    assert completed.completed_at is not None


async def test_complete_requires_required_deliverable(
    session,
    task_factory,
    group_leader_user,
):
    task = await task_factory(status="pending_review")
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="manual.result",
        title="Required result",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    leader = await group_leader_user()
    await _make_leader_authority(session, await _task_event(session, task.id), leader)

    with pytest.raises(MissingRequiredDeliverableError):
        await CollaborationTaskService().complete(
            session,
            task.id,
            leader,
            task.row_version,
        )

    session.add(
        TaskDeliverableVersion(
            deliverable_id=deliverable.id,
            version_no=1,
            source_kind=DeliverableSourceKind.MANUAL,
            text_result={"result": "已完成"},
            created_by=leader.username,
            basis_text="manual result",
        )
    )
    await session.flush()

    completed = await CollaborationTaskService().complete(
        session,
        task.id,
        leader,
        task.row_version,
    )
    assert completed.status == TaskStatus.COMPLETED


async def test_complete_ignores_historical_text_result(
    session,
    task_factory,
    group_leader_user,
):
    task = await task_factory(status="pending_review")
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="manual.result",
        title="Required result",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_FILE_OR_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    leader = await group_leader_user()
    await _make_leader_authority(session, await _task_event(session, task.id), leader)

    old_version = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=1,
        source_kind=DeliverableSourceKind.MANUAL,
        text_result={"result": "old result"},
        created_by=leader.username,
        basis_text="old",
    )
    current_version = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=2,
        source_kind=DeliverableSourceKind.MANUAL,
        storage_key="manual/current.txt",
        file_name="current.txt",
        checksum="checksum",
        mime_type="text/plain",
        size_bytes=1,
        created_by=leader.username,
        basis_text="current file",
    )
    session.add_all([old_version, current_version])
    await session.flush()

    with pytest.raises(MissingRequiredDeliverableError):
        await CollaborationTaskService().complete(
            session,
            task.id,
            leader,
            task.row_version,
        )

    session.add(
        TaskDeliverablePublication(
            deliverable_id=deliverable.id,
            version_id=current_version.id,
            published_by=leader.username,
            published_role="leader",
            published_at=datetime.now(UTC),
        )
    )
    await session.flush()

    completed = await CollaborationTaskService().complete(
        session,
        task.id,
        leader,
        task.row_version,
    )
    assert completed.status == TaskStatus.COMPLETED


async def test_superadmin_can_complete_without_authority(
    session,
    task_factory,
    user_factory,
):
    task = await task_factory(status="pending_review")
    superadmin = await user_factory("admin", "superadmin", None)
    completed = await CollaborationTaskService().complete(
        session,
        task.id,
        superadmin,
        task.row_version,
    )
    assert completed.status == TaskStatus.COMPLETED


async def test_update_changes_fields_and_appends_event(
    session,
    task_factory,
    user_factory,
):
    task = await task_factory(status="in_progress")
    superadmin = await user_factory("update-admin", "superadmin", None)
    due_at = datetime.now(UTC) + timedelta(hours=2)

    updated = await CollaborationTaskService().update(
        session,
        task.id,
        superadmin,
        task.row_version,
        title="Updated title",
        instruction="Updated instruction",
        priority=80,
        due_at=due_at,
    )

    assert updated.title == "Updated title"
    assert updated.instruction == "Updated instruction"
    assert updated.priority == 80
    assert updated.due_at == due_at
    assert updated.row_version == 2

    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
    ).all()
    assert [(item.event_type, item.business_version) for item in events] == [
        ("task_updated", 2)
    ]


async def _task_event(session, task_id) -> EarthquakeEvent:
    task = await session.get(WorkgroupTask, task_id)
    assert task is not None
    event = await session.get(EarthquakeEvent, task.event_id)
    assert event is not None
    return event


async def _client(session, current_user: AuthUser):
    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: current_user
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_task_routes_use_if_match_and_map_conflicts(
    session,
    task_factory,
    group_member_user,
):
    task = await task_factory(workgroup_code="monitoring_forecast")
    member = await group_member_user()
    await _make_member_roster(
        session,
        await _task_event(session, task.id),
        member,
    )
    client = await _client(
        session,
        AuthUser(member.username, member.role, member.workgroup),
    )
    try:
        stale = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/start",
            headers={
                "If-Match": str(task.row_version + 1),
                "Idempotency-Key": "stale-start",
            },
        )
        assert stale.status_code == 409

        first = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/start",
            headers={
                "If-Match": str(task.row_version),
                "Idempotency-Key": "start-once",
            },
        )
        assert first.status_code == 200
        assert first.json()["status"] == "in_progress"
        assert first.json()["row_version"] == 2

        retry = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/start",
            headers={
                "If-Match": "2",
                "Idempotency-Key": "start-once",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["row_version"] == 2
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
    ).all()
    assert len(events) == 1


async def test_task_route_maps_permission_error_to_403(
    session,
    task_factory,
    other_group_user,
):
    task = await task_factory(workgroup_code="monitoring_forecast")
    outsider = await other_group_user()
    client = await _client(
        session,
        AuthUser(outsider.username, outsider.role, outsider.workgroup),
    )
    try:
        response = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/start",
            headers={"If-Match": "1"},
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403


async def test_list_and_detail_task_routes(
    session,
    event_factory,
    task_factory,
    group_member_user,
):
    event = await event_factory()
    first = await task_factory(event=event, workgroup_code="monitoring_forecast")
    second = await task_factory(
        event=event,
        workgroup_code="monitoring_forecast",
        status="in_progress",
    )
    member = await group_member_user()
    await RosterService().snapshot_for_event(session, event.id)
    client = await _client(
        session,
        AuthUser(member.username, member.role, member.workgroup),
    )
    try:
        listed = await client.get(
            f"/api/v1/events/{event.id}/collaboration/tasks"
        )
        detail = await client.get(
            f"/api/v1/collaboration/tasks/{first.id}"
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {str(first.id), str(second.id)}
    assert detail.status_code == 200
    assert detail.json()["id"] == str(first.id)
    assert detail.json()["row_version"] == 1


async def test_task_reads_are_scoped_to_effective_workgroup(
    session,
    event_factory,
    task_factory,
    group_member_user,
    other_group_user,
    user_factory,
):
    event = await event_factory()
    own_task = await task_factory(
        event=event,
        workgroup_code="monitoring_forecast",
    )
    other_task = await task_factory(
        event=event,
        workgroup_code="news_information",
    )
    member = await group_member_user()
    other_member = await other_group_user()
    superadmin = await user_factory("read-admin", "superadmin", None)
    outsider = await user_factory("read-outsider", "viewer", None)
    await RosterService().snapshot_for_event(session, event.id)

    member_client = await _client(
        session,
        AuthUser(member.username, member.role, member.workgroup),
    )
    try:
        listed = await member_client.get(
            f"/api/v1/events/{event.id}/collaboration/tasks"
        )
        own_detail = await member_client.get(
            f"/api/v1/collaboration/tasks/{own_task.id}"
        )
        forbidden_detail = await member_client.get(
            f"/api/v1/collaboration/tasks/{other_task.id}"
        )
    finally:
        await member_client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(own_task.id)]
    assert listed.json()[0]["can_work"] is True
    assert listed.json()[0]["can_confirm"] is False
    assert own_detail.status_code == 200
    assert own_detail.json()["can_work"] is True
    assert own_detail.json()["can_confirm"] is False
    assert forbidden_detail.status_code == 403

    other_client = await _client(
        session,
        AuthUser(
            other_member.username,
            other_member.role,
            other_member.workgroup,
        ),
    )
    try:
        other_listed = await other_client.get(
            f"/api/v1/events/{event.id}/collaboration/tasks"
        )
    finally:
        await other_client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert other_listed.status_code == 200
    assert [item["id"] for item in other_listed.json()] == [
        str(other_task.id)
    ]

    admin_client = await _client(
        session,
        AuthUser(superadmin.username, superadmin.role, None),
    )
    try:
        admin_listed = await admin_client.get(
            f"/api/v1/events/{event.id}/collaboration/tasks"
        )
    finally:
        await admin_client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert admin_listed.status_code == 200
    assert {item["id"] for item in admin_listed.json()} == {
        str(own_task.id),
        str(other_task.id),
    }

    outsider_client = await _client(
        session,
        AuthUser(outsider.username, outsider.role, outsider.workgroup),
    )
    try:
        outsider_listed = await outsider_client.get(
            f"/api/v1/events/{event.id}/collaboration/tasks"
        )
        outsider_detail = await outsider_client.get(
            f"/api/v1/collaboration/tasks/{own_task.id}"
        )
    finally:
        await outsider_client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert outsider_listed.status_code == 200
    assert outsider_listed.json() == []
    assert outsider_detail.status_code == 403


async def test_task_reads_require_matching_event_roster_role(
    session,
    event_factory,
    task_factory,
    group_member_user,
    user_factory,
):
    event = await event_factory()
    task = await task_factory(
        event=event,
        workgroup_code="monitoring_forecast",
    )
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="permission.result",
        title="Permission result",
        is_required=False,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()

    viewer = await user_factory(
        "snapshot-viewer",
        "viewer",
        "monitoring_forecast",
        duty_role="viewer",
        group_code="monitoring_forecast",
    )
    mismatch = await user_factory(
        "snapshot-mismatch",
        "group_member",
        "monitoring_forecast",
        duty_role="member",
        group_code="monitoring_forecast",
    )
    outside = await user_factory(
        "snapshot-outside",
        "group_member",
        "monitoring_forecast",
    )
    member = await group_member_user()
    roster = RosterService()
    await roster.snapshot_for_event(session, event.id)

    mismatch_membership = await session.scalar(
        select(WorkgroupMembership).where(
            WorkgroupMembership.user_id == mismatch.id,
            WorkgroupMembership.workgroup_code == "monitoring_forecast",
            WorkgroupMembership.is_active.is_(True),
            WorkgroupMembership.effective_to.is_(None),
        )
    )
    assert mismatch_membership is not None
    mismatch_membership.duty_role = "leader"
    session.add(
        WorkgroupMembership(
            user_id=outside.id,
            workgroup_code="monitoring_forecast",
            duty_role="member",
            is_active=True,
            created_by="system",
        )
    )
    await session.flush()

    async def read_paths(user: User):
        client = await _client(
            session,
            AuthUser(user.username, user.role, user.workgroup),
        )
        try:
            listed = await client.get(
                f"/api/v1/events/{event.id}/collaboration/tasks"
            )
            detail = await client.get(
                f"/api/v1/collaboration/tasks/{task.id}"
            )
            deliverables = await client.get(
                f"/api/v1/collaboration/tasks/{task.id}/deliverables"
            )
            return listed, detail, deliverables
        finally:
            await client.aclose()
            app.dependency_overrides.pop(get_roster_session, None)
            app.dependency_overrides.pop(get_current_user, None)

    viewer_listed, viewer_detail, viewer_deliverables = await read_paths(
        viewer
    )
    assert viewer_listed.status_code == 200
    assert viewer_listed.json() == []
    assert viewer_detail.status_code == 403
    assert viewer_deliverables.status_code == 403

    mismatch_listed, mismatch_detail, mismatch_deliverables = await read_paths(
        mismatch
    )
    assert mismatch_listed.status_code == 200
    assert mismatch_listed.json() == []
    assert mismatch_detail.status_code == 403
    assert mismatch_deliverables.status_code == 403

    outside_listed, outside_detail, outside_deliverables = await read_paths(
        outside
    )
    assert outside_listed.status_code == 200
    assert outside_listed.json() == []
    assert outside_detail.status_code == 403
    assert outside_deliverables.status_code == 403

    member_listed, member_detail, member_deliverables = await read_paths(
        member
    )
    assert member_listed.status_code == 200
    assert [item["id"] for item in member_listed.json()] == [str(task.id)]
    assert member_detail.status_code == 200
    assert member_deliverables.status_code == 200


async def test_can_confirm_follows_leader_and_deputy_attendance_order(
    session,
    event_factory,
    task_factory,
    user_factory,
):
    event = await event_factory()
    task = await task_factory(
        event=event,
        workgroup_code="monitoring_forecast",
        status="pending_review",
    )
    leader = await user_factory(
        "authority-leader",
        "group_leader",
        "monitoring_forecast",
    )
    first_deputy = await user_factory(
        "authority-deputy-1",
        "group_deputy",
        "monitoring_forecast",
    )
    second_deputy = await user_factory(
        "authority-deputy-2",
        "group_deputy",
        "monitoring_forecast",
    )
    roster = RosterService()
    await roster.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER),
            MemberInput(first_deputy.id, DutyRole.DEPUTY, deputy_order=1),
            MemberInput(second_deputy.id, DutyRole.DEPUTY, deputy_order=2),
        ],
        actor="system",
    )
    await roster.snapshot_for_event(session, event.id)

    async def read_as(user: User):
        client = await _client(
            session,
            AuthUser(user.username, user.role, user.workgroup),
        )
        try:
            return await client.get(
                f"/api/v1/collaboration/tasks/{task.id}"
            )
        finally:
            await client.aclose()
            app.dependency_overrides.pop(get_roster_session, None)
            app.dependency_overrides.pop(get_current_user, None)

    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        leader.id,
        "present",
        "system",
    )
    leader_response = await read_as(leader)
    first_deputy_response = await read_as(first_deputy)
    assert leader_response.json()["can_confirm"] is True
    assert first_deputy_response.json()["can_confirm"] is False

    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        leader.id,
        "absent",
        "system",
    )
    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        first_deputy.id,
        "present",
        "system",
    )
    first_deputy_response = await read_as(first_deputy)
    second_deputy_response = await read_as(second_deputy)
    assert first_deputy_response.json()["can_confirm"] is True
    assert second_deputy_response.json()["can_confirm"] is False

    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        first_deputy.id,
        "absent",
        "system",
    )
    await roster.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        second_deputy.id,
        "present",
        "system",
    )
    first_deputy_response = await read_as(first_deputy)
    second_deputy_response = await read_as(second_deputy)
    assert first_deputy_response.json()["can_confirm"] is False
    assert second_deputy_response.json()["can_confirm"] is True


async def test_complete_route_is_idempotent(
    session,
    task_factory,
    user_factory,
):
    task = await task_factory(status="pending_review")
    superadmin = await user_factory("complete-admin", "superadmin", None)
    client = await _client(
        session,
        AuthUser(superadmin.username, superadmin.role, None),
    )
    try:
        first = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/complete",
            headers={
                "If-Match": str(task.row_version),
                "Idempotency-Key": "complete-once",
            },
        )
        assert first.status_code == 200
        assert first.json()["status"] == "completed"
        assert first.json()["row_version"] == 2

        retry = await client.post(
            f"/api/v1/collaboration/tasks/{task.id}/complete",
            headers={
                "If-Match": "2",
                "Idempotency-Key": "complete-once",
            },
        )
        assert retry.status_code == 200
        assert retry.json()["row_version"] == 2
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    events = (
        await session.scalars(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
    ).all()
    assert len(events) == 1
