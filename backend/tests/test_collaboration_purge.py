from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, event, func, select, text

from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
    CreateProductionRunCommand,
)
from app.artifacts.renderers.base import RenderQuality, RenderResult
from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactTaskActivityInput
from app.assessment.models import AssessmentRun, AssessmentTask
from app.auth.models import User
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    CommandHallAlertProjection,
    CommandHallEventProjection,
    CommandHallGroupProjection,
    NotificationDelivery,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupTask,
    WorkgroupTaskContributor,
)
from app.collaboration.purge import (
    EventNotFoundError,
    EventPurgeReceipt,
    SuperadminPurgeService,
)
from app.collaboration.router import (
    get_artifact_store,
    get_cleanup_session,
    get_purge_session,
)
from app.collaboration.service import DeliverableService
from app.config import settings
from app.data_assets.models import (
    DataAsset,
    DataAssetAuditLog,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.db import engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.event_object_cleanup import (
    CANDIDATE_DELETE_CLEANUP_SOURCE,
    CleanupIntentStatus,
    EventObjectCleanupIntent,
    process_cleanup_intents,
)
from app.main import app


@pytest.fixture(autouse=True)
async def _dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


def _checksum(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PurgeFixture:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    raw_id: uuid.UUID
    assessment_run_id: uuid.UUID
    production_run_id: uuid.UUID
    artifact_id: uuid.UUID
    artifact_path: str
    manual_path: str
    shared_artifact_path: str
    shared_manual_path: str
    task_id: uuid.UUID
    deliverable_id: uuid.UUID
    deliverable_version_id: uuid.UUID
    task_event_id: uuid.UUID
    override_id: uuid.UUID
    second_event_id: uuid.UUID
    second_revision_id: uuid.UUID
    second_raw_id: uuid.UUID
    second_artifact_id: uuid.UUID
    second_manual_version_id: uuid.UUID
    asset_id: uuid.UUID
    asset_version_id: uuid.UUID
    audit_id: uuid.UUID
    contributor_id: uuid.UUID


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
async def purge_fixture(seeded_artifact_assessment, session_factory):
    assessment = seeded_artifact_assessment
    artifact = await assessment.published_artifact("map.epicenter", version=1)
    path_token = uuid.uuid4().hex
    artifact_path = f"fixtures/purge/{assessment.event_id}/{path_token}/map-epicenter.jpg"
    manual_path = f"objects/purge/{assessment.event_id}/{path_token}/event-manual.txt"
    shared_artifact_path = artifact_path
    shared_manual_path = (
        f"objects/purge/second/{assessment.event_id}/{path_token}/shared-manual.txt"
    )
    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            stored_artifact = await session.get(GeneratedArtifact, artifact.id)
            assert stored_artifact is not None
            stored_artifact.storage_path = artifact_path
            first_event = await session.get(EarthquakeEvent, assessment.event_id)
            first_revision = await session.get(EarthquakeRevision, assessment.revision_id)
            assert first_event is not None
            assert first_revision is not None

            contributor = User(
                id=uuid.uuid4(),
                username=f"purge-contributor-{uuid.uuid4().hex[:8]}",
                password_hash="not-used",
                role="group_member",
                workgroup="monitoring_forecast",
                is_active=True,
            )
            session.add(contributor)
            await session.flush()

            task = WorkgroupTask(
                event_id=first_event.id,
                trigger_revision_id=first_revision.id,
                assessment_run_id=assessment.assessment_run_id,
                task_code=f"purge-task-{uuid.uuid4().hex}",
                source_type="ad_hoc",
                workgroup_code="monitoring_forecast",
                title="Hard purge fixture task",
                instruction="Must be removed with the event.",
                priority=100,
                status="completed",
                timeliness_state="on_time",
                phase_code="within_30m",
                activated_at=now,
                due_at=now + timedelta(minutes=30),
                completed_at=now + timedelta(minutes=5),
                closed_at=now + timedelta(minutes=5),
                row_version=1,
                created_by="system",
            )
            session.add(task)
            await session.flush()
            session.add(
                WorkgroupTaskContributor(
                    task_id=task.id,
                    user_id=contributor.id,
                    contribution_count=1,
                    first_contributed_at=now,
                    last_contributed_at=now,
                )
            )
            deliverable = TaskDeliverable(
                task_id=task.id,
                deliverable_code="manual.shared",
                title="Shared manual output",
                is_required=True,
                requirement_kind="manual_file",
                display_order=1,
            )
            session.add(deliverable)
            await session.flush()
            version = TaskDeliverableVersion(
                deliverable_id=deliverable.id,
                version_no=1,
                source_kind="manual",
                storage_key=manual_path,
                file_name="event-manual.txt",
                checksum="a" * 64,
                mime_type="text/plain",
                size_bytes=12,
                created_by=contributor.username,
            )
            session.add(version)
            await session.flush()
            session.add(
                TaskDeliverablePublication(
                    deliverable_id=deliverable.id,
                    version_id=version.id,
                    published_by=contributor.username,
                    published_role="leader",
                    published_at=now,
                )
            )
            task_event = CollaborationTaskEvent(
                task_id=task.id,
                event_seq=1,
                event_type="task_completed",
                actor=contributor.username,
                from_status="in_progress",
                to_status="completed",
                business_version=1,
                idempotency_key=f"purge-log-{uuid.uuid4()}",
                payload={"must_be_deleted": True},
            )
            session.add(task_event)
            session.add(
                NotificationDelivery(
                    event_id=first_event.id,
                    task_id=task.id,
                    recipient_user_id=contributor.id,
                    intent_type="status_changed",
                    channel="in_app",
                    dedupe_key=f"purge-notification-{uuid.uuid4()}",
                    status="sent",
                    available_at=now,
                    sent_at=now,
                )
            )
            session.add(
                CollaborationOutbox(
                    event_id=first_event.id,
                    task_id=task.id,
                    event_type="task.completed",
                    idempotency_key=f"purge-outbox-{uuid.uuid4()}",
                    payload={},
                    status="published",
                    available_at=now,
                    dispatched_at=now,
                )
            )
            session.add_all(
                [
                    CommandHallEventProjection(
                        event_id=first_event.id,
                        event_snapshot={},
                        task_counts={},
                        artifact_summary={},
                        alert_summary={},
                    ),
                    CommandHallGroupProjection(
                        event_id=first_event.id,
                        workgroup_code="monitoring_forecast",
                        group_snapshot={},
                        task_counts={},
                        alert_summary={},
                    ),
                    CommandHallAlertProjection(
                        event_id=first_event.id,
                        workgroup_code="monitoring_forecast",
                        task_id=task.id,
                        alert_key=f"purge-alert-{uuid.uuid4()}",
                        alert_type="overdue",
                        severity="warning",
                        title="Purge me",
                    ),
                ]
            )
            override = ArtifactOverrideRequest(
                actor_id="purge-test",
                endpoint=f"/api/v1/artifacts/{artifact.id}/override",
                idempotency_key=f"purge-override-{uuid.uuid4()}",
                request_fingerprint="b" * 64,
                status="succeeded",
                response_status=200,
                production_run_id=artifact.production_run_id,
                artifact_id=artifact.id,
                event_id=first_event.id,
                created_at=now,
                completed_at=now,
            )
            session.add(override)
            session.add(
                ArtifactOverrideRequest(
                    actor_id="purge-test",
                    endpoint=(
                        f"/api/v1/events/{first_event.id}/artifacts/map.epicenter/"
                        "override?output_profile=a3v-professional"
                    ),
                    idempotency_key=f"purge-failed-override-{uuid.uuid4()}",
                    request_fingerprint="f" * 64,
                    status="failed",
                    response_status=422,
                    response_body={
                        "error_category": "format_mismatch",
                        "summary": "failed before run/artifact binding",
                    },
                    production_run_id=None,
                    artifact_id=None,
                    event_id=first_event.id,
                    created_at=now,
                    completed_at=now,
                )
            )

            asset = DataAsset(
                asset_key=f"purge-global-{uuid.uuid4().hex}",
                region_id=settings.data_asset_region_id,
                name="Global purge test asset",
                data_type="vector",
                spatial_granularity="town",
                responsibility_unit="test",
                update_interval_days=365,
                is_core=True,
                contract={},
            )
            session.add(asset)
            await session.flush()
            asset_version = DataAssetVersion(
                asset_id=asset.id,
                version="v1",
                status="published",
                source_uri="https://example.invalid/purge-asset",
                schema_summary={},
                record_count=0,
                checksum="c" * 64,
                published_at=now,
            )
            session.add(asset_version)
            await session.flush()
            session.add(
                DataAssetSnapshot(
                    run_id=assessment.assessment_run_id,
                    asset_id=asset.id,
                    region_id=settings.data_asset_region_id,
                    asset_version_id=asset_version.id,
                    asset_key=asset.asset_key,
                    version="v1",
                    checksum="c" * 64,
                    role="required",
                    required=True,
                )
            )
            audit = DataAssetAuditLog(
                asset_id=asset.id,
                version_id=asset_version.id,
                action="publish",
                actor="purge-test",
                details={},
            )
            session.add(audit)
            await session.flush()

            second_event_id = uuid.uuid4()
            second_event = EarthquakeEvent(
                id=second_event_id,
                source="purge-shared-test",
                canonical_source_id=f"purge-shared-{second_event_id}",
                event_type="formal",
                origin_time=now,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="shared object event",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="active",
            )
            session.add(second_event)
            await session.flush()
            second_raw = RawMessage(
                source="purge-shared-test",
                source_message_id=f"raw-{uuid.uuid4()}",
                message_kind="formal",
                checksum=_checksum(f"raw-{uuid.uuid4()}"),
                payload={"event_id": str(second_event.id)},
                received_at=now,
            )
            session.add(second_raw)
            await session.flush()
            second_revision = EarthquakeRevision(
                event_id=second_event.id,
                raw_message_id=second_raw.id,
                revision_no=1,
                revision_kind="formal",
                origin_time=second_event.origin_time,
                longitude=second_event.longitude,
                latitude=second_event.latitude,
                depth_km=second_event.depth_km,
                magnitude=second_event.magnitude,
                place=second_event.place,
                is_current=True,
                ingested_at=now,
            )
            session.add(second_revision)
            await session.flush()
            second_event.current_revision_id = second_revision.id

            second_task = WorkgroupTask(
                event_id=second_event.id,
                trigger_revision_id=second_revision.id,
                task_code=f"shared-task-{uuid.uuid4().hex}",
                source_type="ad_hoc",
                workgroup_code="monitoring_forecast",
                title="Shared reference holder",
                instruction="Retain the shared path.",
                priority=100,
                status="completed",
                timeliness_state="on_time",
                activated_at=now,
                due_at=now + timedelta(minutes=30),
                row_version=1,
                created_by="system",
            )
            session.add(second_task)
            await session.flush()
            second_deliverable = TaskDeliverable(
                task_id=second_task.id,
                deliverable_code="manual.shared",
                title="Shared manual output",
                is_required=True,
                requirement_kind="manual_file",
                display_order=1,
            )
            session.add(second_deliverable)
            await session.flush()
            second_manual = TaskDeliverableVersion(
                deliverable_id=second_deliverable.id,
                version_no=1,
                source_kind="manual",
                storage_key=shared_manual_path,
                file_name="shared-manual.txt",
                checksum="a" * 64,
                mime_type="text/plain",
                size_bytes=12,
                created_by="system",
            )
            session.add(second_manual)
            await session.flush()

    repository = ArtifactProductionRepository()
    async with session_factory() as session:
        async with session.begin():
            second_event = await session.get(EarthquakeEvent, second_event_id)
            assert second_event is not None
            second_revision = await session.scalar(
                select(EarthquakeRevision).where(
                    EarthquakeRevision.event_id == second_event_id,
                    EarthquakeRevision.revision_no == 1,
                )
            )
            assert second_revision is not None
            second_run = await repository.create_run(
                session,
                CreateProductionRunCommand(
                    assessment_run_id=None,
                    event_id=second_event.id,
                    revision_id=second_revision.id,
                    revision_no=1,
                    production_mode="live",
                    launch_mode="standalone",
                    deadline_basis_at=now,
                    deadline_at=now + timedelta(seconds=300),
                    deadline_kind="rebuild_deadline",
                    catalog_version=assessment.catalog.catalog_version,
                    generation_scope="artifact:map.epicenter:a3v-professional",
                    required_outputs=(("map.epicenter", "a3v-professional"),),
                    snapshot={"fixture": "shared object"},
                ),
            )
            second_task = await session.scalar(
                select(ProductionTask).where(
                    ProductionTask.production_run_id == second_run.id,
                    ProductionTask.artifact_key == "map.epicenter",
                )
            )
            assert second_task is not None
            await repository.freeze_task_fingerprint(
                session,
                second_task.id,
                _checksum(f"shared-{second_task.id}"),
            )
            await repository.start_task(
                session,
                second_task.id,
                f"fixture:{second_run.id}:{uuid.uuid4()}",
            )
            second_artifact = await repository.complete_task(
                session,
                second_task.id,
                ArtifactGenerationResult(
                    file_name="shared-epicenter.jpg",
                    format="jpg",
                    storage_path=shared_artifact_path,
                    checksum="d" * 64,
                    size_bytes=1024,
                    quality=ArtifactQuality(grade="A", needs_review=False),
                    width=4761,
                    height=3369,
                    generated_at=now,
                ),
                "succeeded",
            )

    fixture = PurgeFixture(
        event_id=assessment.event_id,
        revision_id=assessment.revision_id,
        raw_id=await _scalar_id(
            session_factory,
            select(EarthquakeRevision.raw_message_id).where(
                EarthquakeRevision.id == assessment.revision_id
            ),
        ),
        assessment_run_id=assessment.assessment_run_id,
        production_run_id=artifact.production_run_id,
        artifact_id=artifact.id,
        artifact_path=artifact_path,
        manual_path=manual_path,
        shared_artifact_path=shared_artifact_path,
        shared_manual_path=shared_manual_path,
        task_id=task.id,
        deliverable_id=deliverable.id,
        deliverable_version_id=version.id,
        task_event_id=task_event.id,
        override_id=override.id,
        second_event_id=second_event_id,
        second_revision_id=second_revision.id,
        second_raw_id=second_raw.id,
        second_artifact_id=second_artifact.id,
        second_manual_version_id=second_manual.id,
        asset_id=asset.id,
        asset_version_id=asset_version.id,
        audit_id=audit.id,
        contributor_id=contributor.id,
    )
    try:
        yield fixture
    finally:
        await _cleanup_purge_fixture(session_factory, fixture)


async def _scalar_id(session_factory, statement):
    async with session_factory() as session:
        return await session.scalar(statement)


async def _cleanup_purge_fixture(session_factory, fixture: PurgeFixture) -> None:
    service = SuperadminPurgeService()
    actor = AuthUser(
        username="purge-fixture-cleanup",
        role="superadmin",
        workgroup=None,
    )
    for event_id, key in (
        (fixture.event_id, "purge-fixture-primary-cleanup"),
        (fixture.second_event_id, "purge-fixture-secondary-cleanup"),
    ):
        async with session_factory() as session:
            async with session.begin():
                try:
                    await service.purge_event(
                        session,
                        event_id,
                        actor=actor,
                        idempotency_key=key,
                    )
                except EventNotFoundError:
                    pass

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(EventPurgeReceipt).where(
                    EventPurgeReceipt.event_id.in_((fixture.event_id, fixture.second_event_id))
                )
            )
            await session.execute(delete(DataAsset).where(DataAsset.id == fixture.asset_id))
            await session.execute(delete(User).where(User.id == fixture.contributor_id))


@pytest.fixture
def superadmin_user() -> AuthUser:
    return AuthUser(username="purge-superadmin", role="superadmin", workgroup=None)


@pytest.fixture
def group_leader_user() -> AuthUser:
    return AuthUser(
        username="purge-group-leader",
        role="group_leader",
        workgroup="monitoring_forecast",
    )


async def test_non_superadmin_cannot_purge_event(
    purge_fixture,
    group_leader_user,
    session_factory,
) -> None:
    async with session_factory() as session:
        with pytest.raises(PermissionError):
            await SuperadminPurgeService().purge_event(
                session,
                purge_fixture.event_id,
                actor=group_leader_user,
            )

    async with session_factory() as session:
        assert await session.get(EarthquakeEvent, purge_fixture.event_id) is not None


async def test_superadmin_purge_removes_full_event_closure_and_preserves_shared_assets(
    session,
    purge_fixture,
    superadmin_user,
) -> None:
    result = await SuperadminPurgeService().purge_event(
        session,
        purge_fixture.event_id,
        actor=superadmin_user,
        idempotency_key="purge-full-closure",
    )
    await session.commit()

    assert result.deleted_event_id == purge_fixture.event_id
    assert result.deleted_revision_count == 1
    assert result.deleted_raw_message_count == 1
    assert result.deleted_assessment_run_count == 1
    assert result.deleted_collaboration_task_count == 1
    assert result.deleted_artifact_count == 1
    assert result.deleted_publication_count == 1
    assert result.deleted_override_count == 2
    assert result.deleted_task_event_count == 1
    assert result.already_purged is False
    assert purge_fixture.shared_artifact_path == purge_fixture.artifact_path
    assert purge_fixture.artifact_path in result.storage_paths
    assert purge_fixture.manual_path in result.storage_paths
    assert purge_fixture.shared_artifact_path in result.storage_paths
    assert purge_fixture.shared_manual_path not in result.storage_paths
    assert set(result.storage_paths) == {
        purge_fixture.artifact_path,
        purge_fixture.manual_path,
    }

    assert await session.get(EarthquakeEvent, purge_fixture.event_id) is None
    assert await session.get(EarthquakeRevision, purge_fixture.revision_id) is None
    assert await session.get(RawMessage, purge_fixture.raw_id) is None
    assert await session.get(AssessmentRun, purge_fixture.assessment_run_id) is None
    assert await session.get(GeneratedArtifact, purge_fixture.artifact_id) is None
    assert await session.get(WorkgroupTask, purge_fixture.task_id) is None
    assert (await session.get(TaskDeliverable, purge_fixture.deliverable_id)) is None
    assert (
        await session.get(
            TaskDeliverableVersion,
            purge_fixture.deliverable_version_id,
        )
        is None
    )
    assert (await session.get(CollaborationTaskEvent, purge_fixture.task_event_id)) is None
    assert await session.get(ArtifactOverrideRequest, purge_fixture.override_id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ArtifactOverrideRequest)
            .where(
                ArtifactOverrideRequest.endpoint.like(
                    f"%/events/{purge_fixture.event_id}/artifacts/%"
                )
            )
        )
        == 0
    )
    assert await session.get(ProductionRun, purge_fixture.production_run_id) is None

    child_counts = (
        (
            AssessmentTask,
            AssessmentTask.run_id == purge_fixture.assessment_run_id,
        ),
        (
            ArtifactPublication,
            ArtifactPublication.artifact_id == purge_fixture.artifact_id,
        ),
        (
            TaskDeliverablePublication,
            TaskDeliverablePublication.deliverable_id == purge_fixture.deliverable_id,
        ),
        (
            WorkgroupTaskContributor,
            WorkgroupTaskContributor.task_id == purge_fixture.task_id,
        ),
        (
            ProductionTask,
            ProductionTask.production_run_id == purge_fixture.production_run_id,
        ),
        (
            ProductionInputSnapshot,
            ProductionInputSnapshot.production_run_id == purge_fixture.production_run_id,
        ),
    )
    for model, predicate in child_counts:
        count = await session.scalar(select(func.count()).select_from(model).where(predicate))
        assert count == 0

    assert await session.get(EarthquakeEvent, purge_fixture.second_event_id) is not None
    assert (
        await session.get(
            EarthquakeRevision,
            purge_fixture.second_revision_id,
        )
        is not None
    )
    assert await session.get(RawMessage, purge_fixture.second_raw_id) is not None
    assert await session.get(GeneratedArtifact, purge_fixture.second_artifact_id) is not None
    assert (
        await session.get(
            TaskDeliverableVersion,
            purge_fixture.second_manual_version_id,
        )
        is not None
    )
    assert await session.get(DataAsset, purge_fixture.asset_id) is not None
    assert await session.get(DataAssetVersion, purge_fixture.asset_version_id) is not None
    assert await session.get(DataAssetAuditLog, purge_fixture.audit_id) is not None

    orphan_tables = (
        EventLifecycleOutbox,
        AssessmentRun,
        WorkgroupTask,
        CollaborationOutbox,
        NotificationDelivery,
        CommandHallEventProjection,
        CommandHallGroupProjection,
        CommandHallAlertProjection,
    )
    for model in orphan_tables:
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.event_id == purge_fixture.event_id)
        )
        assert count == 0


async def test_purge_is_idempotent_and_missing_event_is_distinct(
    session,
    purge_fixture,
    superadmin_user,
) -> None:
    first = await SuperadminPurgeService().purge_event(
        session,
        purge_fixture.event_id,
        actor=superadmin_user,
        idempotency_key="same-purge-key",
    )
    await session.commit()
    second_version = await session.get(
        TaskDeliverableVersion,
        purge_fixture.second_manual_version_id,
    )
    assert second_version is not None
    session.add(
        TaskDeliverableVersion(
            deliverable_id=second_version.deliverable_id,
            version_no=2,
            source_kind="manual",
            storage_key=purge_fixture.manual_path,
            file_name="retained-after-purge.txt",
            checksum="e" * 64,
            mime_type="text/plain",
            size_bytes=12,
            created_by="system",
        )
    )
    await session.commit()
    retry = await SuperadminPurgeService().purge_event(
        session,
        purge_fixture.event_id,
        actor=superadmin_user,
        idempotency_key="same-purge-key",
    )
    await session.commit()

    assert retry.already_purged is True
    assert retry.deleted_event_id == first.deleted_event_id
    assert retry.deleted_raw_message_count == first.deleted_raw_message_count
    assert purge_fixture.manual_path in first.storage_paths
    assert purge_fixture.manual_path in retry.storage_paths

    with pytest.raises(EventNotFoundError):
        await SuperadminPurgeService().purge_event(
            session,
            purge_fixture.event_id,
            actor=superadmin_user,
            idempotency_key="different-purge-key",
        )
    await session.rollback()
    with pytest.raises(EventNotFoundError):
        await SuperadminPurgeService().purge_event(
            session,
            uuid.uuid4(),
            actor=superadmin_user,
            idempotency_key="never-existed",
        )
    await session.rollback()


class CommitAwareStore:
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def resolve(self, relative_path: str) -> Path:
        return Path(relative_path)

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        if not self._state["committed"]:
            raise AssertionError("artifact file was deleted before database commit")
        deleted = self._state["deleted"]
        assert isinstance(deleted, list)
        deleted.append(stored.relative_path)


class FailOnceStore(CommitAwareStore):
    def __init__(self, state: dict[str, object]) -> None:
        super().__init__(state)
        self._fail_once = True

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        if self._fail_once:
            self._fail_once = False
            raise OSError("simulated object cleanup failure")
        super().delete_unreferenced(stored)


class FailOnceArtifactStore(ArtifactStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls = 0

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("simulated candidate cleanup failure")
        super().delete_unreferenced(stored)


async def test_http_purge_deletes_files_only_after_commit(
    purge_fixture,
    session_factory,
    superadmin_user,
) -> None:
    state: dict[str, object] = {"committed": False, "deleted": []}
    store = CommitAwareStore(state)

    async def override_session():
        async with session_factory() as session:
            event.listen(
                session.sync_session,
                "after_commit",
                lambda _session: state.__setitem__("committed", True),
            )
            yield session

    app.dependency_overrides[get_purge_session] = override_session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: superadmin_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                headers={"Idempotency-Key": "http-purge-key"},
            )
            assert response.status_code == 204

            retry = await client.delete(
                f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                headers={"Idempotency-Key": "http-purge-key"},
            )
            assert retry.status_code == 204

            missing = await client.delete(
                f"/api/v1/admin/events/{uuid.uuid4()}/purge",
                headers={"Idempotency-Key": "http-missing-key"},
            )
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()

    assert state["committed"] is True
    assert purge_fixture.artifact_path not in state["deleted"]
    assert purge_fixture.manual_path in state["deleted"]
    assert purge_fixture.shared_artifact_path not in state["deleted"]
    assert purge_fixture.shared_manual_path not in state["deleted"]


async def test_http_purge_retries_failed_file_cleanup_after_commit(
    purge_fixture,
    session_factory,
    superadmin_user,
) -> None:
    state: dict[str, object] = {"committed": False, "deleted": []}
    store = FailOnceStore(state)

    async def override_session():
        async with session_factory() as session:
            event.listen(
                session.sync_session,
                "after_commit",
                lambda _session: state.__setitem__("committed", True),
            )
            yield session

    app.dependency_overrides[get_purge_session] = override_session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: superadmin_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            first = await client.delete(
                f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                headers={"Idempotency-Key": "http-cleanup-retry"},
            )
            assert first.status_code == 503

            retry = await client.delete(
                f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                headers={"Idempotency-Key": "http-cleanup-retry"},
            )
            assert retry.status_code == 204
    finally:
        app.dependency_overrides.clear()

    assert state["committed"] is True
    assert state["deleted"] == [purge_fixture.manual_path]


async def test_http_candidate_delete_without_key_recovers_failed_cleanup(
    session_factory,
    superadmin_user,
    tmp_path,
) -> None:
    fixture = await _create_concurrent_deliverable_fixture(
        session_factory,
        label="candidate-http-recovery",
    )
    store = FailOnceArtifactStore(tmp_path / "artifacts")
    service = DeliverableService(artifact_store=store)
    payload = f"candidate-http-recovery-{uuid.uuid4()}".encode()
    file_name = f"candidate-http-recovery-{uuid.uuid4()}.txt"

    async with session_factory() as session:
        async with session.begin():
            version = await service.add_manual_version(
                session,
                fixture.deliverable_id,
                fixture.actor,
                source=BytesIO(payload),
                file_name=file_name,
                mime_type="text/plain",
            )
            version_id = version.id
            storage_path = version.storage_key
            assert storage_path is not None
            path = store.resolve(storage_path)

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_cleanup_session] = override_session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: fixture.actor
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/collaboration/deliverable-versions/{version_id}"
            )
            assert response.status_code == 503
            retry = await client.delete(f"/api/v1/collaboration/deliverable-versions/{version_id}")
            assert retry.status_code == 404
    finally:
        app.dependency_overrides.clear()

    assert path.is_file()
    async with session_factory() as session:
        assert await session.get(TaskDeliverableVersion, version_id) is None
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_kind == CANDIDATE_DELETE_CLEANUP_SOURCE,
                EventObjectCleanupIntent.source_key == str(version_id),
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.PENDING.value
    assert intent.attempt_count == 1
    assert intent.last_error

    recovered = await process_cleanup_intents(
        session_factory,
        store,
        source_kind=CANDIDATE_DELETE_CLEANUP_SOURCE,
        source_key=str(version_id),
        lease_owner="candidate-http-recovery-worker",
    )
    assert recovered.failed_count == 0
    assert not path.exists()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_kind == CANDIDATE_DELETE_CLEANUP_SOURCE,
                EventObjectCleanupIntent.source_key == str(version_id),
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.COMPLETED.value


@dataclass(frozen=True, slots=True)
class ConcurrentDeliverableFixture:
    event_id: uuid.UUID
    raw_id: uuid.UUID
    revision_id: uuid.UUID
    task_id: uuid.UUID
    deliverable_id: uuid.UUID
    actor: AuthUser


async def _create_concurrent_deliverable_fixture(
    session_factory,
    *,
    label: str,
) -> ConcurrentDeliverableFixture:
    now = datetime.now(UTC)
    username = f"purge-concurrent-{label}-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        async with session.begin():
            actor = User(
                id=uuid.uuid4(),
                username=username,
                password_hash="not-used",
                role="superadmin",
                workgroup=None,
                is_active=True,
            )
            session.add(actor)
            event_id = uuid.uuid4()
            earthquake_event = EarthquakeEvent(
                id=event_id,
                source="purge-concurrency-test",
                canonical_source_id=f"purge-concurrent-{event_id}",
                event_type="formal",
                origin_time=now,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="purge concurrency fixture",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="active",
            )
            session.add(earthquake_event)
            await session.flush()
            raw = RawMessage(
                source="purge-concurrency-test",
                source_message_id=f"raw-{uuid.uuid4()}",
                message_kind="formal",
                checksum=_checksum(f"raw-{uuid.uuid4()}"),
                payload={"event_id": str(event_id)},
                received_at=now,
            )
            session.add(raw)
            await session.flush()
            revision = EarthquakeRevision(
                event_id=event_id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="formal",
                origin_time=earthquake_event.origin_time,
                longitude=earthquake_event.longitude,
                latitude=earthquake_event.latitude,
                depth_km=earthquake_event.depth_km,
                magnitude=earthquake_event.magnitude,
                place=earthquake_event.place,
                is_current=True,
                ingested_at=now,
            )
            session.add(revision)
            await session.flush()
            earthquake_event.current_revision_id = revision.id
            task = WorkgroupTask(
                event_id=event_id,
                trigger_revision_id=revision.id,
                task_code=f"purge-concurrent-task-{uuid.uuid4().hex}",
                source_type="ad_hoc",
                workgroup_code="monitoring_forecast",
                title="Concurrent purge task",
                instruction="Exercise the event write barrier.",
                priority=100,
                status="in_progress",
                timeliness_state="on_time",
                phase_code="within_30m",
                activated_at=now,
                due_at=now + timedelta(minutes=30),
                row_version=1,
                created_by=username,
            )
            session.add(task)
            await session.flush()
            deliverable = TaskDeliverable(
                task_id=task.id,
                deliverable_code="concurrent.manual",
                title="Concurrent manual output",
                is_required=True,
                requirement_kind="manual_file",
                display_order=1,
            )
            session.add(deliverable)
            await session.flush()
            return ConcurrentDeliverableFixture(
                event_id=event_id,
                raw_id=raw.id,
                revision_id=revision.id,
                task_id=task.id,
                deliverable_id=deliverable.id,
                actor=AuthUser(
                    username=actor.username,
                    role=actor.role,
                    workgroup=actor.workgroup,
                ),
            )


async def _purge_test_event(
    session_factory,
    event_id: uuid.UUID,
    *,
    key: str,
) -> None:
    actor = AuthUser(
        username="purge-concurrency-cleanup",
        role="superadmin",
        workgroup=None,
    )
    async with session_factory() as session:
        async with session.begin():
            await SuperadminPurgeService().purge_event(
                session,
                event_id,
                actor=actor,
                idempotency_key=key,
            )


async def _wait_for_advisory_waiter(
    session_factory,
    *,
    timeout_seconds: float = 1.0,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as session:
            waiting = int(
                await session.scalar(
                    select(func.count())
                    .select_from(text("pg_locks"))
                    .where(
                        text("locktype = 'advisory'"),
                        text("granted IS FALSE"),
                    )
                )
                or 0
            )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    return False


class PausingDeliverableService(DeliverableService):
    def __init__(
        self,
        *,
        stored: asyncio.Event,
        release: asyncio.Event,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._stored = stored
        self._release = release

    async def _create_version(self, *args, **kwargs):
        self._stored.set()
        await self._release.wait()
        return await super()._create_version(*args, **kwargs)


async def test_purge_does_not_delete_path_committed_by_concurrent_other_event(
    purge_fixture,
    session_factory,
    superadmin_user,
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    token = uuid.uuid4().hex
    payload = f"cross-event-shared-object-{token}".encode()
    file_name = f"shared-object-{token}.txt"
    relative_path = store.relative_path_for(
        hashlib.sha256(payload).hexdigest(),
        file_name=file_name,
    )
    store.store_immutable_stream(BytesIO(payload), file_name=file_name)

    async with session_factory() as session:
        async with session.begin():
            artifact = await session.get(
                GeneratedArtifact,
                purge_fixture.artifact_id,
            )
            assert artifact is not None
            artifact.storage_path = relative_path
            second_artifact = await session.get(
                GeneratedArtifact,
                purge_fixture.second_artifact_id,
            )
            assert second_artifact is not None
            second_artifact.storage_path = f"{relative_path}.other-event"

    concurrent = await _create_concurrent_deliverable_fixture(
        session_factory,
        label="other-event",
    )
    stored = asyncio.Event()
    release = asyncio.Event()
    service = PausingDeliverableService(
        stored=stored,
        release=release,
        artifact_store=store,
    )

    async def write_other_event() -> None:
        async with session_factory() as session:
            async with session.begin():
                await service.add_manual_version(
                    session,
                    concurrent.deliverable_id,
                    concurrent.actor,
                    source=BytesIO(payload),
                    file_name=file_name,
                    mime_type="text/plain",
                )

    writer = asyncio.create_task(write_other_event())
    await asyncio.wait_for(stored.wait(), timeout=2)
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: superadmin_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            purge = asyncio.create_task(
                client.delete(
                    f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                    headers={"Idempotency-Key": "cross-event-purge"},
                )
            )
            await _wait_for_advisory_waiter(session_factory)
            release.set()
            response = await asyncio.wait_for(purge, timeout=5)
            assert response.status_code == 204
    finally:
        app.dependency_overrides.clear()
        release.set()
        await asyncio.wait_for(writer, timeout=5)

    async with session_factory() as session:
        version_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.deliverable_id == concurrent.deliverable_id,
                    TaskDeliverableVersion.storage_key == relative_path,
                )
            )
            or 0
        )
    assert version_count == 1
    assert store.resolve(relative_path).is_file()


async def test_purge_removes_artifact_stored_by_concurrent_same_event_render(
    purge_fixture,
    seeded_artifact_assessment,
    session_factory,
    superadmin_user,
    tmp_path,
    monkeypatch,
) -> None:
    repository = ArtifactProductionRepository()
    async with session_factory() as session:
        async with session.begin():
            run = await repository.create_run(
                session,
                seeded_artifact_assessment.full_run_command(
                    assessment_run_id=None,
                    launch_mode="standalone",
                    generation_scope="artifact:map.epicenter:a3v-professional",
                    required_outputs=(("map.epicenter", "a3v-professional"),),
                ),
            )
            task = await session.scalar(
                select(ProductionTask).where(
                    ProductionTask.production_run_id == run.id,
                    ProductionTask.artifact_key == "map.epicenter",
                )
            )
            assert task is not None
            await repository.freeze_task_fingerprint(
                session,
                task.id,
                _checksum(f"concurrent-render-{task.id}"),
            )
            await repository.start_task(
                session,
                task.id,
                f"concurrent-render:{task.id}",
            )
            task_id = task.id
            run_id = run.id

    payload = b"concurrent-render-output"
    staged_path = tmp_path / "staged-map.jpg"
    staged_path.write_bytes(payload)
    store = ArtifactStore(tmp_path / "artifacts")
    checksum = hashlib.sha256(payload).hexdigest()
    relative_path = store.relative_path_for(
        checksum,
        file_name="concurrent-map.jpg",
    )
    render_result = RenderResult(
        path=staged_path,
        format="jpg",
        width=100,
        height=80,
        dpi=72,
        checksum=checksum,
        quality=RenderQuality(grade="A", needs_review=False),
        task_status="succeeded",
        file_name="concurrent-map.jpg",
        render_manifest={"marker": "concurrency"},
        non_empty_ratio=0.5,
        size_bytes=len(payload),
        generated_at=datetime.now(UTC),
    )
    activities = ArtifactActivities(
        session_factory,
        render_concurrency=1,
        store=store,
        heartbeat_interval=60,
    )

    async def fake_render(self, request, renderer):
        del self, request, renderer
        return staged_path, "concurrent-map.jpg", render_result

    monkeypatch.setattr(ArtifactActivities, "_render_map", fake_render)
    complete_started = asyncio.Event()
    release_complete = asyncio.Event()
    original_complete = ArtifactProductionRepository.complete_task

    async def pause_complete(
        self,
        session,
        task_id,
        result,
        task_status,
    ):
        complete_started.set()
        await release_complete.wait()
        return await original_complete(
            self,
            session,
            task_id,
            result,
            task_status,
        )

    monkeypatch.setattr(
        ArtifactProductionRepository,
        "complete_task",
        pause_complete,
    )
    request = ArtifactTaskActivityInput(
        production_run_id=str(run_id),
        production_task_id=str(task_id),
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        context_fingerprint="c" * 64,
        input_fingerprint="d" * 64,
        deadline_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    activity = asyncio.create_task(activities.render_map_artifact(request))
    await asyncio.wait_for(complete_started.wait(), timeout=2)
    assert store.resolve(relative_path).is_file()

    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: superadmin_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            purge = asyncio.create_task(
                client.delete(
                    f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                    headers={"Idempotency-Key": "same-event-render-purge"},
                )
            )
            await _wait_for_advisory_waiter(session_factory)
            release_complete.set()
            response = await asyncio.wait_for(purge, timeout=5)
            assert response.status_code == 204
    finally:
        app.dependency_overrides.clear()
        release_complete.set()
        with suppress(Exception):
            await asyncio.wait_for(activity, timeout=5)

    assert not store.resolve(relative_path).exists()


async def test_purge_first_makes_inflight_manual_write_fail_without_orphan(
    purge_fixture,
    session_factory,
    superadmin_user,
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"event-private-after-purge"
    relative_path = store.relative_path_for(
        hashlib.sha256(payload).hexdigest(),
        file_name="after-purge.txt",
    )
    username = f"purge-late-writer-{uuid.uuid4().hex[:8]}"
    actor = User(
        id=uuid.uuid4(),
        username=username,
        password_hash="not-used",
        role="superadmin",
        workgroup=None,
        is_active=True,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(actor)

    started = asyncio.Event()
    release = asyncio.Event()
    service = PausingDeliverableService(
        stored=started,
        release=release,
        artifact_store=store,
    )

    original_add_manual_version = service.add_manual_version

    async def pause_before_write(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_add_manual_version(*args, **kwargs)

    service.add_manual_version = pause_before_write

    async def late_write() -> None:
        async with session_factory() as session:
            async with session.begin():
                await service.add_manual_version(
                    session,
                    purge_fixture.deliverable_id,
                    AuthUser(
                        username=actor.username,
                        role=actor.role,
                        workgroup=actor.workgroup,
                    ),
                    source=BytesIO(payload),
                    file_name="after-purge.txt",
                    mime_type="text/plain",
                )

    writer = asyncio.create_task(late_write())
    await asyncio.wait_for(started.wait(), timeout=2)
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: superadmin_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.delete(
                f"/api/v1/admin/events/{purge_fixture.event_id}/purge",
                headers={"Idempotency-Key": "purge-before-manual-write"},
            )
            assert response.status_code == 204
        release.set()
        with pytest.raises(LookupError):
            await asyncio.wait_for(writer, timeout=5)
    finally:
        app.dependency_overrides.clear()
        release.set()
        with suppress(Exception):
            await asyncio.wait_for(writer, timeout=5)

    assert not store.resolve(relative_path).exists()


async def test_candidate_delete_returns_postcommit_cleanup_intent_and_rollback_keeps_file(
    session_factory,
    tmp_path,
) -> None:
    fixture = await _create_concurrent_deliverable_fixture(
        session_factory,
        label="candidate-rollback",
    )
    store = ArtifactStore(tmp_path / "artifacts")
    service = DeliverableService(artifact_store=store)
    payload = f"rollback-candidate-{uuid.uuid4()}".encode()

    async with session_factory() as session:
        async with session.begin():
            version = await service.add_manual_version(
                session,
                fixture.deliverable_id,
                fixture.actor,
                source=BytesIO(payload),
                file_name="rollback-candidate.txt",
                mime_type="text/plain",
            )
            version_id = version.id
            storage_path = version.storage_key
            assert storage_path is not None
            path = store.resolve(storage_path)
            assert path.is_file()

    async with session_factory() as session:
        transaction = await session.begin()
        cleanup = await service.delete_candidate(
            session,
            version_id,
            fixture.actor,
        )
        assert cleanup is not None
        assert cleanup.event_id == fixture.event_id
        assert cleanup.storage_paths == (storage_path,)
        assert path.is_file()
        await transaction.rollback()

    assert path.is_file()
    async with session_factory() as session:
        assert await session.get(TaskDeliverableVersion, version_id) is not None


async def test_candidate_delete_waits_for_cross_event_same_path_writer(
    session_factory,
    tmp_path,
) -> None:
    first = await _create_concurrent_deliverable_fixture(
        session_factory,
        label="candidate-cross-event-a",
    )
    second = await _create_concurrent_deliverable_fixture(
        session_factory,
        label="candidate-cross-event-b",
    )
    store = ArtifactStore(tmp_path / "artifacts")
    writer_service = DeliverableService(artifact_store=store)

    async with session_factory() as session:
        async with session.begin():
            version = await writer_service.add_manual_version(
                session,
                first.deliverable_id,
                first.actor,
                source=BytesIO(b"cross-event-candidate"),
                file_name="cross-event-candidate.txt",
                mime_type="text/plain",
            )
            version_id = version.id
            storage_path = version.storage_key
            assert storage_path is not None
            path = store.resolve(storage_path)
            assert path.is_file()

    stored = asyncio.Event()
    release = asyncio.Event()
    pausing_writer = PausingDeliverableService(
        stored=stored,
        release=release,
        artifact_store=store,
    )

    async def write_second_event() -> None:
        async with session_factory() as session:
            async with session.begin():
                await pausing_writer.add_manual_version(
                    session,
                    second.deliverable_id,
                    second.actor,
                    source=BytesIO(b"cross-event-candidate"),
                    file_name="cross-event-candidate.txt",
                    mime_type="text/plain",
                )

    writer = asyncio.create_task(write_second_event())
    await asyncio.wait_for(stored.wait(), timeout=2)
    delete_result: list[object] = []

    async def delete_first_event_candidate() -> None:
        async with session_factory() as session:
            async with session.begin():
                cleanup = await writer_service.delete_candidate(
                    session,
                    version_id,
                    first.actor,
                )
                delete_result.append(cleanup)

    deleter = asyncio.create_task(delete_first_event_candidate())
    await _wait_for_advisory_waiter(session_factory)
    release.set()
    await asyncio.wait_for(asyncio.gather(deleter, writer), timeout=5)

    assert delete_result == [None]
    assert path.is_file()
    async with session_factory() as session:
        second_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.deliverable_id == second.deliverable_id,
                    TaskDeliverableVersion.storage_key == storage_path,
                )
            )
            or 0
        )
    assert second_count == 1
