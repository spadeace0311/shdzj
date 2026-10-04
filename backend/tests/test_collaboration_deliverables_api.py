import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.artifacts.storage import ArtifactStore
from app.auth.models import User
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.domain import (
    DeliverableRequirementKind,
    DeliverableSourceKind,
    DutyRole,
)
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupMembership,
    WorkgroupTask,
)
from app.collaboration.roster import MemberInput, RosterService
from app.collaboration.router import get_cleanup_session, get_roster_session
from app.collaboration.service import DeliverableService
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
from app.event_object_cleanup import cleanup_event_objects
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
    async def factory() -> EarthquakeEvent:
        event_id = uuid.uuid4()
        event = EarthquakeEvent(
            id=event_id,
            source="collaboration-deliverable-test",
            canonical_source_id=f"deliverable-{event_id}",
            event_type="formal",
            origin_time=datetime.now(UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="collaboration deliverable fixture",
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
            source="collaboration-deliverable-test",
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
    ) -> WorkgroupTask:
        if event is None:
            event = await event_factory()
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
            title="Task deliverable fixture",
            instruction="Process the deliverable.",
            priority=100,
            status=status,
            timeliness_state="on_time",
            phase_code="within_30m",
            activated_at=now,
            due_at=now + timedelta(hours=1),
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
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            username=f"deliverable-{label}-{uuid.uuid4().hex[:8]}",
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
                    deputy_order=1 if duty_role == "deputy" else None,
                    is_active=True,
                    created_by="system",
                )
            )
            await session.flush()
        return user

    return factory


@pytest.fixture
async def group_member_user(user_factory):
    return await user_factory(
        "member",
        "group_member",
        "monitoring_forecast",
        duty_role="member",
        group_code="monitoring_forecast",
    )


@pytest.fixture
async def group_leader_user(user_factory):
    return await user_factory(
        "leader",
        "group_leader",
        "monitoring_forecast",
        duty_role="leader",
        group_code="monitoring_forecast",
    )


@pytest.fixture
async def group_deputy_user(user_factory):
    return await user_factory(
        "deputy",
        "group_deputy",
        "monitoring_forecast",
        duty_role="deputy",
        group_code="monitoring_forecast",
    )


@pytest.fixture
async def superadmin_user(user_factory):
    return await user_factory("admin", "superadmin", None)


async def _make_group_roster(
    session,
    event: EarthquakeEvent,
    leader: User,
    member: User,
    deputy: User,
) -> None:
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER),
            MemberInput(member.id, DutyRole.MEMBER),
            MemberInput(deputy.id, DutyRole.DEPUTY, deputy_order=1),
        ],
        actor="system",
    )
    await service.snapshot_for_event(session, event.id)


@pytest.fixture
async def task_with_automatic_deliverable(
    session,
    task_factory,
    group_leader_user,
    group_member_user,
    group_deputy_user,
):
    task = await task_factory(status="pending_review")
    await _make_group_roster(
        session,
        await _task_event(session, task.id),
        group_leader_user,
        group_member_user,
        group_deputy_user,
    )
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="result.v1",
        title="Result deliverable",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_FILE_OR_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    automatic = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=1,
        source_kind=DeliverableSourceKind.AUTOMATIC,
        text_result={"result": "automatic baseline"},
        created_by="system",
        basis_text="automatic artifact baseline",
    )
    session.add(automatic)
    await session.flush()
    deliverable.candidate_version_id = automatic.id
    return deliverable


@pytest.fixture
async def deliverable(
    session,
    task_factory,
    group_leader_user,
    group_member_user,
    group_deputy_user,
):
    task = await task_factory(status="pending_review")
    await _make_group_roster(
        session,
        await _task_event(session, task.id),
        group_leader_user,
        group_member_user,
        group_deputy_user,
    )
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="result.v1",
        title="Result deliverable",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    candidate = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=1,
        source_kind=DeliverableSourceKind.MANUAL,
        text_result={"result": "candidate"},
        created_by="member",
        basis_text="candidate",
    )
    session.add(candidate)
    await session.flush()
    deliverable.candidate_version_id = candidate.id
    return deliverable


async def _task_event(session, task_id) -> EarthquakeEvent:
    task = await session.get(WorkgroupTask, task_id)
    assert task is not None
    event = await session.get(EarthquakeEvent, task.event_id)
    assert event is not None
    return event


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


async def _make_deputy_authority(
    session,
    event: EarthquakeEvent,
    leader: User,
    deputy: User,
) -> None:
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER),
            MemberInput(deputy.id, DutyRole.DEPUTY, deputy_order=1),
        ],
        actor="system",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session,
        event.id,
        "monitoring_forecast",
        deputy.id,
        "present",
        "system",
    )


async def test_automatic_and_manual_versions_coexist_with_one_current_publication(
    session,
    task_with_automatic_deliverable,
    group_leader_user,
):
    deliverable = task_with_automatic_deliverable
    service = DeliverableService()
    manual = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_leader_user,
        text_result={"result": "manual revision"},
        basis_text="revised automatic model result",
    )
    await _make_leader_authority(
        session,
        await _task_event(session, deliverable.task_id),
        group_leader_user,
    )

    publication = await service.publish(
        session,
        deliverable.id,
        version_id=manual.id,
        actor=group_leader_user,
    )

    versions = (
        await session.scalars(
            select(TaskDeliverableVersion)
            .where(TaskDeliverableVersion.deliverable_id == deliverable.id)
            .order_by(TaskDeliverableVersion.version_no)
        )
    ).all()
    assert [version.source_kind for version in versions] == ["automatic", "manual"]
    assert publication.version_id == manual.id
    assert await service.current_version_id(session, deliverable.id) == manual.id


async def test_member_cannot_publish_deliverable(
    session,
    deliverable,
    group_member_user,
):
    with pytest.raises(PermissionError):
        await DeliverableService().publish(
            session,
            deliverable.id,
            version_id=deliverable.candidate_version_id,
            actor=group_member_user,
        )


async def test_deputy_can_publish_when_leader_is_absent(
    session,
    deliverable,
    group_leader_user,
    group_deputy_user,
):
    await _make_deputy_authority(
        session,
        await _task_event(session, deliverable.task_id),
        group_leader_user,
        group_deputy_user,
    )
    publication = await DeliverableService().publish(
        session,
        deliverable.id,
        version_id=deliverable.candidate_version_id,
        actor=group_deputy_user,
    )
    assert publication.published_role == "deputy"


async def test_superadmin_can_publish_without_authority(
    session,
    deliverable,
    superadmin_user,
):
    publication = await DeliverableService().publish(
        session,
        deliverable.id,
        version_id=deliverable.candidate_version_id,
        actor=superadmin_user,
    )
    assert publication.published_role == "superadmin"


async def test_restore_republishes_historical_version_and_supersedes_current(
    session,
    deliverable,
    superadmin_user,
):
    service = DeliverableService()
    first_version_id = deliverable.candidate_version_id
    second = await service.add_text_version(
        session,
        deliverable.id,
        actor=superadmin_user,
        text_result={"result": "second"},
        basis_text="second candidate",
    )
    await service.publish(
        session,
        deliverable.id,
        version_id=second.id,
        actor=superadmin_user,
    )

    restored = await service.restore(
        session,
        deliverable.id,
        version_id=first_version_id,
        actor=superadmin_user,
    )

    assert restored.version_id == first_version_id
    assert await service.current_version_id(session, deliverable.id) == first_version_id
    publications = (
        await session.scalars(
            select(TaskDeliverablePublication)
            .where(TaskDeliverablePublication.deliverable_id == deliverable.id)
            .order_by(TaskDeliverablePublication.published_at)
        )
    ).all()
    assert len(publications) == 2
    assert publications[0].superseded_at is not None
    assert publications[1].superseded_at is None


async def test_override_creates_superadmin_override_and_publishes(
    session,
    deliverable,
    superadmin_user,
):
    service = DeliverableService()
    publication = await service.override(
        session,
        deliverable.id,
        actor=superadmin_user,
        text_result={"result": "superadmin override"},
        basis_text="corrected externally",
    )

    version = await session.get(TaskDeliverableVersion, publication.version_id)
    assert version is not None
    assert version.source_kind == DeliverableSourceKind.SUPERADMIN_OVERRIDE
    assert publication.published_role == "superadmin"
    assert await service.current_version_id(session, deliverable.id) == version.id


async def test_publish_rejects_version_from_other_deliverable(
    session,
    deliverable,
    superadmin_user,
):
    other_task = await _new_task_for_deliverable(session)
    other = TaskDeliverable(
        task_id=other_task.id,
        deliverable_code="other.result",
        title="Other result",
        is_required=False,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    session.add(other)
    await session.flush()
    version = TaskDeliverableVersion(
        deliverable_id=other.id,
        version_no=1,
        source_kind=DeliverableSourceKind.MANUAL,
        text_result={"result": "other"},
        created_by=superadmin_user.username,
    )
    session.add(version)
    await session.flush()

    with pytest.raises(ValueError):
        await DeliverableService().publish(
            session,
            deliverable.id,
            version_id=version.id,
            actor=superadmin_user,
        )


async def test_publish_rejects_missing_storage_object(
    session,
    deliverable,
    superadmin_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    stored = store.store_immutable_stream(BytesIO(b"file-content"), file_name="result.txt")
    version = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=2,
        source_kind=DeliverableSourceKind.MANUAL,
        storage_key=stored.relative_path,
        file_name=stored.file_name,
        checksum=stored.checksum,
        mime_type="text/plain",
        size_bytes=stored.size_bytes,
        created_by=superadmin_user.username,
    )
    session.add(version)
    await session.flush()
    stored.managed_path.unlink()

    with pytest.raises(ValueError):
        await DeliverableService(artifact_store=store).publish(
            session,
            deliverable.id,
            version_id=version.id,
            actor=superadmin_user,
        )


async def test_publish_rejects_checksum_mismatch(
    session,
    deliverable,
    superadmin_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    stored = store.store_immutable_stream(BytesIO(b"file-content"), file_name="result.txt")
    version = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=2,
        source_kind=DeliverableSourceKind.MANUAL,
        storage_key=stored.relative_path,
        file_name=stored.file_name,
        checksum="0" * 64,
        mime_type="text/plain",
        size_bytes=stored.size_bytes,
        created_by=superadmin_user.username,
    )
    session.add(version)
    await session.flush()

    with pytest.raises(ValueError):
        await DeliverableService(artifact_store=store).publish(
            session,
            deliverable.id,
            version_id=version.id,
            actor=superadmin_user,
        )


async def test_add_manual_version_stores_file_and_records_metadata(
    session,
    deliverable,
    group_member_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    service = DeliverableService(artifact_store=store)

    version = await service.add_manual_version(
        session,
        deliverable.id,
        actor=group_member_user,
        source=BytesIO(b"manual-bytes"),
        file_name="result.txt",
        mime_type="text/plain",
        basis_text="uploaded result",
    )

    assert version.source_kind == DeliverableSourceKind.MANUAL
    assert version.storage_key is not None
    assert version.size_bytes == len(b"manual-bytes")
    assert (
        version.checksum
        == store.store_immutable_stream(BytesIO(b"manual-bytes"), file_name="result.txt").checksum
    )
    assert store.resolve(version.storage_key).is_file()


async def test_add_manual_version_rejects_path_escape_and_oversized_upload(
    session,
    deliverable,
    group_member_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=4)
    service = DeliverableService(artifact_store=store)

    with pytest.raises(ValueError):
        await service.add_manual_version(
            session,
            deliverable.id,
            actor=group_member_user,
            source=BytesIO(b"safe"),
            file_name="../escape.txt",
            mime_type="text/plain",
        )

    with pytest.raises(ValueError):
        await service.add_manual_version(
            session,
            deliverable.id,
            actor=group_member_user,
            source=BytesIO(b"too-large"),
            file_name="large.txt",
            mime_type="text/plain",
        )


async def test_delete_candidate_allows_uploader_and_rejects_published_or_automatic(
    session,
    deliverable,
    group_member_user,
    group_leader_user,
):
    service = DeliverableService()
    member_version = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_member_user,
        text_result={"result": "member candidate"},
        basis_text="member uploaded",
    )

    await service.delete_candidate(
        session,
        member_version.id,
        actor=group_member_user,
    )
    assert await session.get(TaskDeliverableVersion, member_version.id) is None

    published_version = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_leader_user,
        text_result={"result": "leader candidate"},
        basis_text="leader candidate",
    )
    await _make_leader_authority(
        session,
        await _task_event(session, deliverable.task_id),
        group_leader_user,
    )
    await service.publish(
        session,
        deliverable.id,
        version_id=published_version.id,
        actor=group_leader_user,
    )
    with pytest.raises(ValueError):
        await service.delete_candidate(
            session,
            published_version.id,
            actor=group_leader_user,
        )

    automatic = (
        await session.scalars(
            select(TaskDeliverableVersion)
            .where(
                TaskDeliverableVersion.deliverable_id == deliverable.id,
                TaskDeliverableVersion.source_kind == DeliverableSourceKind.AUTOMATIC,
            )
            .limit(1)
        )
    ).first()
    if automatic is not None:
        with pytest.raises(ValueError):
            await service.delete_candidate(
                session,
                automatic.id,
                actor=group_leader_user,
            )


async def test_leader_can_delete_member_candidate(
    session,
    deliverable,
    group_member_user,
    group_leader_user,
):
    service = DeliverableService()
    candidate = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_member_user,
        text_result={"result": "member candidate"},
        basis_text="member candidate",
    )
    await _make_leader_authority(
        session,
        await _task_event(session, deliverable.task_id),
        group_leader_user,
        group_member_user,
    )

    await service.delete_candidate(
        session,
        candidate.id,
        actor=group_leader_user,
    )
    assert await session.get(TaskDeliverableVersion, candidate.id) is None


async def test_delete_candidate_rejects_automatic_version(
    session,
    task_with_automatic_deliverable,
    group_leader_user,
):
    deliverable = task_with_automatic_deliverable
    automatic = (
        await session.scalars(
            select(TaskDeliverableVersion)
            .where(
                TaskDeliverableVersion.deliverable_id == deliverable.id,
                TaskDeliverableVersion.source_kind == DeliverableSourceKind.AUTOMATIC,
            )
            .limit(1)
        )
    ).first()
    assert automatic is not None

    with pytest.raises(ValueError):
        await DeliverableService().delete_candidate(
            session,
            automatic.id,
            actor=group_leader_user,
        )


async def test_version_and_publish_retries_are_idempotent_and_append_events(
    session,
    task_with_automatic_deliverable,
    group_leader_user,
):
    deliverable = task_with_automatic_deliverable
    service = DeliverableService()
    first = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_leader_user,
        text_result={"result": "idempotent candidate"},
        basis_text="first upload",
        idempotency_key="deliverable-version-once",
    )
    retry = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_leader_user,
        text_result={"result": "ignored retry payload"},
        basis_text="retry",
        idempotency_key="deliverable-version-once",
    )
    assert retry.id == first.id

    await _make_leader_authority(
        session,
        await _task_event(session, deliverable.task_id),
        group_leader_user,
    )
    publication = await service.publish(
        session,
        deliverable.id,
        version_id=first.id,
        actor=group_leader_user,
        idempotency_key="deliverable-publish-once",
    )
    replay = await service.publish(
        session,
        deliverable.id,
        version_id=first.id,
        actor=group_leader_user,
        idempotency_key="deliverable-publish-once",
    )
    assert replay.id == publication.id

    task = await session.get(WorkgroupTask, deliverable.task_id)
    assert task is not None
    events = (
        await session.scalars(
            select(CollaborationTaskEvent)
            .where(CollaborationTaskEvent.task_id == task.id)
            .order_by(CollaborationTaskEvent.event_seq)
        )
    ).all()
    assert [event.event_type for event in events] == [
        "deliverable_version_added",
        "deliverable_published",
    ]
    outbox = (
        await session.scalars(
            select(CollaborationOutbox)
            .where(CollaborationOutbox.task_id == task.id)
            .order_by(CollaborationOutbox.created_at)
        )
    ).all()
    assert [item.event_type for item in outbox] == [
        "deliverable_version_added",
        "deliverable_published",
    ]


async def test_idempotency_key_rejects_other_deliverable_and_version(
    session,
    task_factory,
    superadmin_user,
):
    task = await task_factory(status="pending_review")
    first = TaskDeliverable(
        task_id=task.id,
        deliverable_code="first.result",
        title="First result",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=1,
    )
    second = TaskDeliverable(
        task_id=task.id,
        deliverable_code="second.result",
        title="Second result",
        is_required=True,
        requirement_kind=DeliverableRequirementKind.MANUAL_TEXT,
        display_order=2,
    )
    session.add_all([first, second])
    await session.flush()

    service = DeliverableService()
    first_version = await service.add_text_version(
        session,
        first.id,
        actor=superadmin_user,
        text_result={"result": "first candidate"},
        idempotency_key="shared-add-key",
    )
    with pytest.raises(ValueError):
        await service.add_text_version(
            session,
            second.id,
            actor=superadmin_user,
            text_result={"result": "wrong deliverable"},
            idempotency_key="shared-add-key",
        )

    second_version = await service.add_text_version(
        session,
        second.id,
        actor=superadmin_user,
        text_result={"result": "second candidate"},
    )
    await service.publish(
        session,
        first.id,
        version_id=first_version.id,
        actor=superadmin_user,
        idempotency_key="shared-publish-key",
    )
    with pytest.raises(ValueError):
        await service.publish(
            session,
            second.id,
            version_id=second_version.id,
            actor=superadmin_user,
            idempotency_key="shared-publish-key",
        )


async def test_delete_retry_after_success_is_idempotent(
    session,
    deliverable,
    group_member_user,
):
    service = DeliverableService()
    version = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_member_user,
        text_result={"result": "delete me"},
        basis_text="delete candidate",
    )

    await service.delete_candidate(
        session,
        version.id,
        actor=group_member_user,
        idempotency_key="delete-once",
    )
    await service.delete_candidate(
        session,
        version.id,
        actor=group_member_user,
        idempotency_key="delete-once",
    )
    assert await session.get(TaskDeliverableVersion, version.id) is None


async def test_delete_candidate_cleans_unreferenced_manual_object(
    session,
    deliverable,
    group_member_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    service = DeliverableService(artifact_store=store)
    version = await service.add_manual_version(
        session,
        deliverable.id,
        actor=group_member_user,
        source=BytesIO(b"delete-me"),
        file_name="delete.txt",
        mime_type="text/plain",
    )
    path = store.resolve(version.storage_key)
    assert path.is_file()

    cleanup = await service.delete_candidate(
        session,
        version.id,
        actor=group_member_user,
    )
    assert cleanup is not None
    assert path.is_file()
    result = await cleanup_event_objects(
        session,
        cleanup.event_id,
        cleanup.storage_paths,
        store,
    )
    assert result.succeeded
    assert not path.exists()


async def test_delete_candidate_keeps_shared_storage_object(
    session,
    deliverable,
    group_member_user,
    tmp_path,
):
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    service = DeliverableService(artifact_store=store)
    first = await service.add_manual_version(
        session,
        deliverable.id,
        actor=group_member_user,
        source=BytesIO(b"shared-content"),
        file_name="shared.txt",
        mime_type="text/plain",
    )
    second = await service.add_manual_version(
        session,
        deliverable.id,
        actor=group_member_user,
        source=BytesIO(b"shared-content"),
        file_name="shared.txt",
        mime_type="text/plain",
    )
    assert first.storage_key == second.storage_key
    path = store.resolve(first.storage_key)

    cleanup = await service.delete_candidate(
        session,
        first.id,
        actor=group_member_user,
    )
    assert cleanup is None
    assert path.is_file()

    cleanup = await service.delete_candidate(
        session,
        second.id,
        actor=group_member_user,
    )
    assert cleanup is not None
    result = await cleanup_event_objects(
        session,
        cleanup.event_id,
        cleanup.storage_paths,
        store,
    )
    assert result.succeeded
    assert not path.exists()


async def test_deliverable_list_and_publish_routes(
    session,
    deliverable,
    superadmin_user,
):
    task = await session.get(WorkgroupTask, deliverable.task_id)
    assert task is not None
    client = await _client(
        session,
        AuthUser(superadmin_user.username, superadmin_user.role, None),
    )
    try:
        listed = await client.get(f"/api/v1/collaboration/tasks/{deliverable.task_id}/deliverables")
        assert listed.status_code == 200
        assert listed.json()[0]["versions"][0]["version_no"] == 1

        created = await client.post(
            f"/api/v1/collaboration/deliverables/{deliverable.id}/versions",
            json={"text_result": {"result": "api text"}, "basis_text": "api"},
            headers={"Idempotency-Key": "api-version"},
        )
        assert created.status_code == 200
        version_id = created.json()["id"]

        published = await client.post(
            f"/api/v1/collaboration/deliverables/{deliverable.id}/publish",
            json={"version_id": version_id, "publication_note": "published"},
            headers={
                "If-Match": str(task.row_version),
                "Idempotency-Key": "api-publish",
            },
        )
        assert published.status_code == 200
        assert published.json()["version_id"] == version_id
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)


async def _new_task_for_deliverable(session) -> WorkgroupTask:
    event_id = uuid.uuid4()
    event = EarthquakeEvent(
        id=event_id,
        source="collaboration-deliverable-test",
        canonical_source_id=f"other-{event_id}",
        event_type="formal",
        origin_time=datetime.now(UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="other deliverable fixture",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="active",
    )
    raw = RawMessage(
        id=uuid.uuid4(),
        source="collaboration-deliverable-test",
        source_message_id=uuid.uuid4().hex,
        message_kind="formal",
        checksum=uuid.uuid4().hex,
        payload={"event_id": str(event.id)},
    )
    session.add_all([event, raw])
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
    task = WorkgroupTask(
        event_id=event.id,
        trigger_revision_id=revision.id,
        task_code=f"other-task-{uuid.uuid4().hex}",
        source_type="ad_hoc",
        workgroup_code="monitoring_forecast",
        title="Other deliverable task",
        instruction="Other.",
        priority=100,
        status="pending_review",
        timeliness_state="on_time",
        phase_code="within_30m",
        activated_at=datetime.now(UTC),
        due_at=datetime.now(UTC) + timedelta(hours=1),
        row_version=1,
        created_by="system",
    )
    session.add(task)
    await session.flush()
    return task


async def _client(session, current_user: AuthUser):
    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    app.dependency_overrides[get_cleanup_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: current_user
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")
