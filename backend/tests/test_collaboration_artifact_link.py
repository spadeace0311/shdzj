import hashlib
import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.artifacts.catalog import load_catalog
from app.artifacts.models import (
    ArtifactPublication,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
    CreateProductionRunCommand,
)
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactPublicationInput
from app.collaboration.artifact_link import ArtifactLinkService
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskTemplate,
    CollaborationTaskTemplateVersion,
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.config import settings
from app.db import engine
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    RawMessage,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@dataclass(frozen=True, slots=True)
class _TaskFixture:
    task_id: uuid.UUID
    template_id: uuid.UUID
    template_version_id: uuid.UUID
    deliverable_id: uuid.UUID | None = None
    deliverable_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class _OtherPublicationFixture:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    production_run_id: uuid.UUID
    artifact_id: uuid.UUID
    publication_id: uuid.UUID


@pytest.fixture
async def session(seeded_artifact_assessment, task_factory):
    local_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(local_engine, expire_on_commit=False)
    try:
        async with session_factory() as local_session:
            transaction = await local_session.begin()
            try:
                yield local_session
            finally:
                await transaction.rollback()
    finally:
        await local_engine.dispose()


@pytest.fixture
async def task_factory(seeded_artifact_assessment, session_factory):
    persisted_templates: list[tuple[uuid.UUID, uuid.UUID]] = []
    persisted_task_ids: list[uuid.UUID] = []

    async def factory(
        *,
        artifact_key: str | None = None,
        output_profile: str | None = None,
        required_deliverables: tuple[str, ...] = (),
        create_deliverables: bool = False,
    ) -> _TaskFixture:
        now = datetime.now(UTC)
        code_suffix = uuid.uuid4().hex
        template_code = f"artifact-link-template-{code_suffix}"
        version = f"artifact-link-version-{code_suffix}"
        task_code = f"artifact-link-task-{code_suffix}"
        bindings = (
            [
                {
                    "artifact_key": artifact_key,
                    "output_profile": output_profile,
                }
            ]
            if artifact_key is not None and output_profile is not None
            else []
        )
        async with session_factory() as persisted_session:
            async with persisted_session.begin():
                template = CollaborationTaskTemplate(
                    code=template_code,
                    title="Artifact link fixture",
                    category="test",
                    workgroup_code="damage_assessment",
                    is_active=True,
                )
                persisted_session.add(template)
                await persisted_session.flush()

                template_version = CollaborationTaskTemplateVersion(
                    template_id=template.id,
                    version=version,
                    template_code=template_code,
                    phase_code="within_30m",
                    start_offset_seconds=0,
                    due_offset_seconds=3600,
                    priority=100,
                    required_deliverables=list(required_deliverables),
                    optional_deliverables=[],
                    artifact_bindings=bindings,
                    applicability={},
                    instruction="",
                    created_by="system",
                )
                persisted_session.add(template_version)
                await persisted_session.flush()

                task = WorkgroupTask(
                    event_id=seeded_artifact_assessment.event_id,
                    trigger_revision_id=seeded_artifact_assessment.revision_id,
                    template_version_id=template_version.id,
                    task_code=task_code,
                    source_type="preplan",
                    workgroup_code="damage_assessment",
                    title="Artifact link fixture",
                    instruction="",
                    priority=100,
                    status="pending",
                    timeliness_state="on_time",
                    phase_code="within_30m",
                    activated_at=now,
                    due_at=now + timedelta(hours=1),
                    row_version=1,
                    created_by="system",
                    created_at=now,
                    updated_at=now,
                )
                persisted_session.add(task)
                await persisted_session.flush()

                deliverable_ids: list[uuid.UUID] = []
                if create_deliverables:
                    if bindings:
                        binding = bindings[0]
                        deliverable = TaskDeliverable(
                            task_id=task.id,
                            deliverable_code=binding["artifact_key"],
                            title=binding["artifact_key"],
                            is_required=True,
                            requirement_kind="automatic_artifact",
                            artifact_binding=binding,
                            display_order=0,
                        )
                        persisted_session.add(deliverable)
                        await persisted_session.flush()
                        deliverable_ids.append(deliverable.id)
                    for display_order, code in enumerate(
                        required_deliverables,
                        start=len(deliverable_ids),
                    ):
                        deliverable = TaskDeliverable(
                            task_id=task.id,
                            deliverable_code=code,
                            title=code,
                            is_required=True,
                            requirement_kind="manual_file_or_text",
                            artifact_binding=None,
                            display_order=display_order,
                        )
                        persisted_session.add(deliverable)
                        await persisted_session.flush()
                        deliverable_ids.append(deliverable.id)

                persisted_templates.append((template_version.id, template.id))
                persisted_task_ids.append(task.id)
                return _TaskFixture(
                    task_id=task.id,
                    template_id=template.id,
                    template_version_id=template_version.id,
                    deliverable_id=(
                        deliverable_ids[0] if deliverable_ids else None
                    ),
                    deliverable_ids=tuple(deliverable_ids),
                )

    yield factory

    async with session_factory() as cleanup_session:
        async with cleanup_session.begin():
            for task_id in persisted_task_ids:
                await cleanup_session.execute(
                    delete(WorkgroupTask).where(WorkgroupTask.id == task_id)
                )
            for version_id, template_id in persisted_templates:
                await cleanup_session.execute(
                    delete(CollaborationTaskTemplateVersion).where(
                        CollaborationTaskTemplateVersion.id == version_id
                    )
                )
                await cleanup_session.execute(
                    delete(CollaborationTaskTemplate).where(
                        CollaborationTaskTemplate.id == template_id
                    )
                )


async def _published_artifact(
    session,
    seeded_artifact_assessment,
    *,
    version: int = 1,
) -> tuple[GeneratedArtifact, ArtifactPublication]:
    artifact = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=version,
    )
    publication = await session.scalar(
        select(ArtifactPublication).where(
            ArtifactPublication.artifact_id == artifact.id,
            ArtifactPublication.production_run_id == artifact.production_run_id,
        )
    )
    assert publication is not None
    return artifact, publication


async def _add_task_for_event(
    session,
    *,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_key: str,
    output_profile: str,
    create_deliverable: bool,
) -> _TaskFixture:
    now = datetime.now(UTC)
    code_suffix = uuid.uuid4().hex
    template_code = f"local-link-template-{code_suffix}"
    template = CollaborationTaskTemplate(
        code=template_code,
        title="Local artifact link fixture",
        category="test",
        workgroup_code="damage_assessment",
        is_active=True,
    )
    session.add(template)
    await session.flush()

    binding = {
        "artifact_key": artifact_key,
        "output_profile": output_profile,
    }
    template_version = CollaborationTaskTemplateVersion(
        template_id=template.id,
        version=f"local-link-version-{code_suffix}",
        template_code=template_code,
        phase_code="within_30m",
        start_offset_seconds=0,
        due_offset_seconds=3600,
        priority=100,
        required_deliverables=[],
        optional_deliverables=[],
        artifact_bindings=[binding],
        applicability={},
        instruction="",
        created_by="system",
    )
    session.add(template_version)
    await session.flush()

    task = WorkgroupTask(
        event_id=event_id,
        trigger_revision_id=revision_id,
        template_version_id=template_version.id,
        task_code=f"local-link-task-{code_suffix}",
        source_type="preplan",
        workgroup_code="damage_assessment",
        title="Local artifact link fixture",
        instruction="",
        priority=100,
        status="pending",
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

    deliverable_id: uuid.UUID | None = None
    if create_deliverable:
        deliverable = TaskDeliverable(
            task_id=task.id,
            deliverable_code=artifact_key,
            title=artifact_key,
            is_required=True,
            requirement_kind="automatic_artifact",
            artifact_binding=binding,
            display_order=0,
        )
        session.add(deliverable)
        await session.flush()
        deliverable_id = deliverable.id

    return _TaskFixture(
        task_id=task.id,
        template_id=template.id,
        template_version_id=template_version.id,
        deliverable_id=deliverable_id,
        deliverable_ids=((deliverable_id,) if deliverable_id is not None else ()),
    )


async def _seed_other_event_publication(session) -> _OtherPublicationFixture:
    now = datetime.now(UTC)
    suffix = uuid.uuid4().hex
    raw = RawMessage(
        id=uuid.uuid4(),
        source="artifact-link-test",
        source_message_id=f"other-{suffix}",
        message_kind="formal",
        checksum=hashlib.sha256(f"other-{suffix}".encode()).hexdigest(),
        payload={},
        received_at=now,
    )
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source="artifact-link-test",
        canonical_source_id=f"artifact-link-{suffix}",
        event_type="formal",
        origin_time=now - timedelta(minutes=2),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="artifact link other event",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="active",
    )
    session.add_all([raw, event])
    await session.flush()
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event.id,
        raw_message_id=raw.id,
        revision_no=1,
        revision_kind="formal",
        source_event_id=raw.source_message_id,
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        ingested_at=now,
        is_current=True,
    )
    session.add(revision)
    await session.flush()
    event.current_revision_id = revision.id

    catalog = load_catalog(settings.artifact_catalog_path)
    repository = ArtifactProductionRepository(catalog)
    run = await repository.create_run(
        session,
        CreateProductionRunCommand(
            assessment_run_id=None,
            event_id=event.id,
            revision_id=revision.id,
            revision_no=1,
            production_mode="live",
            launch_mode="standalone",
            deadline_basis_at=now,
            deadline_at=now + timedelta(seconds=300),
            deadline_kind="rebuild_deadline",
            catalog_version=catalog.catalog_version,
            generation_scope="artifact:map.epicenter:a3v-professional",
            required_outputs=(("map.epicenter", "a3v-professional"),),
            snapshot={},
        ),
    )
    task = await session.scalar(
        select(ProductionTask).where(
            ProductionTask.production_run_id == run.id,
            ProductionTask.artifact_key == "map.epicenter",
        )
    )
    assert task is not None
    fingerprint = hashlib.sha256(f"other-{run.id}".encode()).hexdigest()
    await repository.freeze_task_fingerprint(session, task.id, fingerprint)
    await repository.start_task(
        session,
        task.id,
        f"other-publication:{run.id}:{fingerprint}",
    )
    artifact = await repository.complete_task(
        session,
        task.id,
        ArtifactGenerationResult(
            file_name="map.epicenter-other.jpg",
            format="jpg",
            storage_path=f"fixtures/artifact-link/{suffix}.jpg",
            checksum=hashlib.sha256(f"other-artifact-{suffix}".encode()).hexdigest(),
            size_bytes=1024,
            quality=ArtifactQuality(grade="A", needs_review=False),
            generated_at=now,
        ),
        "succeeded",
    )
    publication = await repository.publish_artifact(
        session,
        artifact.id,
        published_by="artifact-link-test",
        forced=False,
    )
    return _OtherPublicationFixture(
        event_id=event.id,
        revision_id=revision.id,
        production_run_id=run.id,
        artifact_id=artifact.id,
        publication_id=publication.id,
    )


async def test_published_artifact_links_to_matching_deliverable_without_copying_file(
    session,
    seeded_artifact_assessment,
    task_factory,
) -> None:
    task_fixture = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    artifact, publication = await _published_artifact(
        session,
        seeded_artifact_assessment,
    )

    result = await ArtifactLinkService().sync_run_publications(
        session,
        artifact.production_run_id,
    )

    assert result.linked_count == 1
    version = await session.scalar(
        select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.deliverable_id
            == task_fixture.deliverable_id
        )
    )
    assert version is not None
    assert version.source_kind == "automatic"
    assert version.artifact_id == artifact.id
    assert version.artifact_publication_id == publication.id
    assert version.storage_key is None
    assert version.file_name is None
    assert version.mime_type is None
    assert version.size_bytes is None
    assert version.checksum == artifact.checksum


async def test_missing_deliverables_are_materialized_and_linked_once(
    session,
    seeded_artifact_assessment,
    task_factory,
) -> None:
    task_fixture = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        required_deliverables=("manual.summary",),
    )
    artifact, publication = await _published_artifact(
        session,
        seeded_artifact_assessment,
    )

    result = await ArtifactLinkService().sync_run_publications(
        session,
        artifact.production_run_id,
    )

    assert result.created_deliverable_count == 2
    assert result.linked_count == 1
    deliverables = (
        await session.scalars(
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id == task_fixture.task_id)
            .order_by(TaskDeliverable.display_order)
        )
    ).all()
    assert {item.deliverable_code for item in deliverables} == {
        "map.epicenter",
        "manual.summary",
    }
    automatic = next(
        item for item in deliverables if item.deliverable_code == "map.epicenter"
    )
    manual = next(
        item for item in deliverables if item.deliverable_code == "manual.summary"
    )
    assert automatic.title == "map.epicenter"
    assert automatic.is_required is True
    assert automatic.requirement_kind == "automatic_artifact"
    assert automatic.artifact_binding == {
        "artifact_key": "map.epicenter",
        "output_profile": "a3v-professional",
    }
    assert manual.title == "manual.summary"
    assert manual.is_required is True
    assert manual.requirement_kind == "manual_file_or_text"
    assert manual.artifact_binding is None

    version = await session.scalar(
        select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.deliverable_id == automatic.id,
            TaskDeliverableVersion.artifact_publication_id == publication.id,
        )
    )
    assert version is not None

    outbox = (
        await session.scalars(
            select(CollaborationOutbox).where(
                CollaborationOutbox.event_id
                == seeded_artifact_assessment.event_id
            )
        )
    ).all()
    assert len(outbox) == 1
    assert outbox[0].event_type == "artifact_linked"
    assert outbox[0].idempotency_key is not None


async def test_repeated_sync_does_not_create_duplicate_version_or_outbox(
    session,
    seeded_artifact_assessment,
    task_factory,
) -> None:
    task_fixture = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    artifact, _publication = await _published_artifact(
        session,
        seeded_artifact_assessment,
    )

    first = await ArtifactLinkService().sync_run_publications(
        session,
        artifact.production_run_id,
    )
    second = await ArtifactLinkService().sync_run_publications(
        session,
        artifact.production_run_id,
    )

    assert first.linked_count == 1
    assert second.linked_count == 0
    version_count = await session.scalar(
        select(func.count())
        .select_from(TaskDeliverableVersion)
        .where(
            TaskDeliverableVersion.deliverable_id
            == task_fixture.deliverable_id
        )
    )
    outbox_count = await session.scalar(
        select(func.count())
        .select_from(CollaborationOutbox)
        .where(
            CollaborationOutbox.event_id
            == seeded_artifact_assessment.event_id
        )
    )
    assert version_count == 1
    assert outbox_count == 1


async def test_concurrent_syncs_are_idempotent(
    seeded_artifact_assessment,
    task_factory,
    session_factory,
) -> None:
    task_fixture = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    artifact = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )

    async def sync_once():
        async with session_factory() as session:
            async with session.begin():
                return await ArtifactLinkService().sync_run_publications(
                    session,
                    artifact.production_run_id,
                )

    first, second = await asyncio.gather(sync_once(), sync_once())

    assert {first.linked_count, second.linked_count} == {0, 1}
    async with session_factory() as session:
        version_count = await session.scalar(
            select(func.count())
            .select_from(TaskDeliverableVersion)
            .where(
                TaskDeliverableVersion.deliverable_id
                == task_fixture.deliverable_id
            )
        )
        deliverable_count = await session.scalar(
            select(func.count())
            .select_from(TaskDeliverable)
            .where(TaskDeliverable.task_id == task_fixture.task_id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(CollaborationOutbox)
            .where(
                CollaborationOutbox.event_id
                == seeded_artifact_assessment.event_id
            )
        )
    assert version_count == 1
    assert deliverable_count == 1
    assert outbox_count == 1


async def test_artifact_key_and_output_profile_select_the_only_match(
    session,
    seeded_artifact_assessment,
    task_factory,
) -> None:
    matching = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    wrong_profile = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-summary",
        create_deliverables=True,
    )
    wrong_key = await task_factory(
        artifact_key="doc.rapid_report",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    artifact, publication = await _published_artifact(
        session,
        seeded_artifact_assessment,
    )

    result = await ArtifactLinkService().sync_run_publications(
        session,
        artifact.production_run_id,
    )

    assert result.linked_count == 1
    versions = (
        await session.scalars(
            select(TaskDeliverableVersion).where(
                TaskDeliverableVersion.deliverable_id.in_(
                    (
                        matching.deliverable_id,
                        wrong_profile.deliverable_id,
                        wrong_key.deliverable_id,
                    )
                )
            )
        )
    ).all()
    assert len(versions) == 1
    assert versions[0].deliverable_id == matching.deliverable_id
    assert versions[0].artifact_publication_id == publication.id


async def test_matching_uses_event_and_publication_identity(
    session,
    seeded_artifact_assessment,
    task_factory,
) -> None:
    other_event_task = await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverables=True,
    )
    other = await _seed_other_event_publication(session)
    local_task = await _add_task_for_event(
        session,
        event_id=other.event_id,
        revision_id=other.revision_id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        create_deliverable=True,
    )

    result = await ArtifactLinkService().sync_run_publications(
        session,
        other.production_run_id,
    )

    assert result.linked_count == 1
    local_version = await session.scalar(
        select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.deliverable_id == local_task.deliverable_id
        )
    )
    other_event_version = await session.scalar(
        select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.deliverable_id
            == other_event_task.deliverable_id
        )
    )
    assert local_version is not None
    assert local_version.artifact_id == other.artifact_id
    assert local_version.artifact_publication_id == other.publication_id
    assert other_event_version is None


async def test_publish_artifact_production_materializes_and_links_deliverable(
    seeded_artifact_assessment,
    task_factory,
    session_factory,
) -> None:
    await task_factory(
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
    )
    artifact = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
    )
    async with session_factory() as setup_session:
        run = await setup_session.get(ProductionRun, artifact.production_run_id)
        assert run is not None
        observed_at = run.deadline_at

    activities = ArtifactActivities(session_factory)
    result = await activities.publish_artifact_production(
        ArtifactPublicationInput(
            production_run_id=str(artifact.production_run_id),
            observed_at=observed_at.isoformat(),
            published_by="artifact-link-integration",
        )
    )

    assert result["publication_status"] == "completed"
    async with session_factory() as verify_session:
        deliverable = await verify_session.scalar(
            select(TaskDeliverable).where(
                TaskDeliverable.deliverable_code == "map.epicenter",
                TaskDeliverable.requirement_kind == "automatic_artifact",
            )
        )
        assert deliverable is not None
        version = await verify_session.scalar(
            select(TaskDeliverableVersion).where(
                TaskDeliverableVersion.deliverable_id == deliverable.id
            )
        )
        assert version is not None
        assert version.source_kind == "automatic"
        assert version.artifact_id == artifact.id
        assert version.storage_key is None
        assert version.checksum == artifact.checksum
        outbox = await verify_session.scalar(
            select(CollaborationOutbox).where(
                CollaborationOutbox.event_id
                == seeded_artifact_assessment.event_id
            )
        )
        assert outbox is not None
