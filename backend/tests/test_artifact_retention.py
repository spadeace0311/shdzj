from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.artifacts import worker as artifact_worker
from app.artifacts.models import ArtifactPublication, GeneratedArtifact
from app.artifacts.retention import ArtifactRetentionResult, ArtifactRetentionService
from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.collaboration.models import (
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.config import settings
from app.event_object_cleanup import (
    ARTIFACT_RETENTION_CLEANUP_SOURCE,
    CleanupIntentStatus,
    EventObjectCleanupIntent,
    enqueue_object_cleanup_intent,
    process_cleanup_intents,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_retention_tests():
    from app.db import SessionFactory, engine

    await engine.dispose()
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(EventObjectCleanupIntent))
    yield
    await engine.dispose()


async def test_retention_removes_expired_test_and_drill_but_not_live(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    test_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    drill_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="drill",
        age_days=731,
    )
    live_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="live",
        age_days=10_000,
    )

    removed = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
        storage_namespace=ArtifactStore(settings.artifact_storage_root).storage_namespace,
    )

    assert removed.test_deleted == 1
    assert removed.drill_deleted == 1
    assert removed.live_deleted == 0
    assert await session.get(type(test_artifact), test_artifact.id) is None
    assert await session.get(type(drill_artifact), drill_artifact.id) is None
    assert await session.get(type(live_artifact), live_artifact.id) is not None


async def _publish_artifact(
    seeded_artifact_assessment,
    session,
    artifact,
    *,
    superseded: bool,
) -> ArtifactPublication:
    from app.artifacts.models import ProductionRun

    run = await session.get(ProductionRun, artifact.production_run_id)
    assert run is not None
    publication = ArtifactPublication(
        event_id=artifact.event_id,
        revision_id=artifact.revision_id,
        revision_no=run.revision_no,
        production_mode=artifact.production_mode,
        artifact_key=artifact.artifact_key,
        output_profile=artifact.output_profile,
        artifact_id=artifact.id,
        production_run_id=run.id,
        generation_seq=run.generation_seq,
        published_at=artifact.generated_at,
        superseded_at=(seeded_artifact_assessment.now if superseded else None),
    )
    session.add(publication)
    await session.flush()
    return publication


async def test_retention_protects_current_and_superseded_publications(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    current = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    superseded = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=402,
    )
    current_publication = await _publish_artifact(
        seeded_artifact_assessment,
        session,
        current,
        superseded=False,
    )
    superseded_publication = await _publish_artifact(
        seeded_artifact_assessment,
        session,
        superseded,
        superseded=True,
    )

    result = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
        storage_namespace=ArtifactStore(settings.artifact_storage_root).storage_namespace,
    )

    assert result.test_deleted == 0
    assert result.protected_publication_count == 2
    assert dict(result.protected_publication_reasons) == {
        "current_publication": 1,
        "superseded_publication": 1,
    }
    assert await session.get(type(current), current.id) is not None
    assert await session.get(type(superseded), superseded.id) is not None
    assert (
        await session.get(
            ArtifactPublication,
            current_publication.id,
        )
        is not None
    )
    assert (
        await session.get(
            ArtifactPublication,
            superseded_publication.id,
        )
        is not None
    )


async def test_retention_preserves_shared_object_referenced_by_live_artifact(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    expired = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    survivor = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=2,
    )
    stored_survivor = await session.get(type(survivor), survivor.id)
    assert stored_survivor is not None
    stored_survivor.storage_path = expired.storage_path
    await session.flush()
    store = ArtifactStore(settings.artifact_storage_root)
    path = store.resolve(expired.storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"shared-object")

    result = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
        storage_namespace=store.storage_namespace,
    )

    assert result.test_deleted == 1
    assert await session.get(type(expired), expired.id) is None
    assert await session.get(type(survivor), survivor.id) is not None
    assert path.is_file()
    assert path.read_bytes() == b"shared-object"


async def test_retention_uses_cross_table_reference_count_for_manual_version(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
    session_factory,
) -> None:
    expired = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = ArtifactStore(settings.artifact_storage_root)
    path = store.resolve(expired.storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"manual-shared-object")
    now = datetime.now(UTC)
    task = WorkgroupTask(
        event_id=expired.event_id,
        trigger_revision_id=expired.revision_id,
        task_code=f"retention-manual-{uuid.uuid4().hex}",
        source_type="ad_hoc",
        workgroup_code="monitoring_forecast",
        title="Retention manual reference",
        instruction="Keep the shared object.",
        priority=100,
        status="completed",
        timeliness_state="on_time",
        activated_at=now,
        due_at=now + timedelta(minutes=30),
        row_version=1,
        created_by="system",
    )
    session.add(task)
    await session.flush()
    deliverable = TaskDeliverable(
        task_id=task.id,
        deliverable_code="retention.manual",
        title="Retention manual output",
        is_required=True,
        requirement_kind="manual_file",
        display_order=1,
    )
    session.add(deliverable)
    await session.flush()
    manual = TaskDeliverableVersion(
        deliverable_id=deliverable.id,
        version_no=1,
        source_kind="manual",
        storage_key=expired.storage_path,
        file_name="manual-shared.txt",
        checksum="f" * 64,
        mime_type="text/plain",
        size_bytes=len(b"manual-shared-object"),
        created_by="system",
    )
    session.add(manual)
    await session.flush()

    result = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
        storage_namespace=store.storage_namespace,
    )

    assert result.test_deleted == 1
    async with session_factory() as count_session:
        intent_count = int(
            await count_session.scalar(
                select(func.count())
                .select_from(EventObjectCleanupIntent)
                .where(
                    EventObjectCleanupIntent.event_id == expired.event_id,
                    EventObjectCleanupIntent.storage_path == expired.storage_path,
                )
            )
            or 0
        )
    assert intent_count == 0
    assert await session.get(GeneratedArtifact, expired.id) is None
    assert await session.get(TaskDeliverableVersion, manual.id) is not None
    assert path.is_file()
    assert path.read_bytes() == b"manual-shared-object"


async def test_retention_defers_unlink_until_db_commit_and_rollback_keeps_file(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = ArtifactStore(settings.artifact_storage_root)
    path = store.resolve(artifact.storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"retention-rollback-object")

    async with session_factory() as session:
        transaction = await session.begin()
        result = await ArtifactRetentionService().retain_expired(
            session,
            observed_at=seeded_artifact_assessment.now,
            storage_namespace=store.storage_namespace,
        )
        assert result.test_deleted == 1
        intent_count = int(
            await session.scalar(
                select(func.count())
                .select_from(EventObjectCleanupIntent)
                .where(
                    EventObjectCleanupIntent.event_id == artifact.event_id,
                    EventObjectCleanupIntent.storage_path == artifact.storage_path,
                )
            )
            or 0
        )
        assert intent_count == 1
        assert path.is_file()
        await transaction.rollback()

    assert path.is_file()
    assert path.read_bytes() == b"retention-rollback-object"
    async with session_factory() as session:
        assert await session.get(GeneratedArtifact, artifact.id) is not None
        intent_count = int(
            await session.scalar(
                select(func.count())
                .select_from(EventObjectCleanupIntent)
                .where(
                    EventObjectCleanupIntent.event_id == artifact.event_id,
                    EventObjectCleanupIntent.storage_path == artifact.storage_path,
                )
            )
            or 0
        )
    assert intent_count == 0


class FlakyCleanupStore(ArtifactStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.calls = 0

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("injected object deletion failure")
        super().delete_unreferenced(stored)


async def test_retention_cleanup_can_retry_same_intent_after_unlink_failure(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = FlakyCleanupStore(tmp_path / "artifacts")
    source_path = store.resolve(artifact.storage_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"retention-cleanup-retry")
    relative_path = artifact.storage_path

    async with session_factory() as session:
        async with session.begin():
            await ArtifactRetentionService().retain_expired(
                session,
                observed_at=seeded_artifact_assessment.now,
                storage_namespace=store.storage_namespace,
            )

    first = await process_cleanup_intents(
        session_factory,
        store,
        source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
        source_key=str(artifact.id),
        lease_owner="retention-retry-one",
    )
    assert first.failed_count == 1
    assert source_path.is_file()

    second = await process_cleanup_intents(
        session_factory,
        store,
        source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
        source_key=str(artifact.id),
        lease_owner="retention-retry-two",
    )
    assert second.failed_count == 0
    assert not source_path.exists()
    assert relative_path


async def test_retention_reports_postcommit_cleanup_intent(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )

    result = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
        storage_namespace=ArtifactStore(settings.artifact_storage_root).storage_namespace,
    )

    assert result.test_deleted == 1
    assert result.failed_deletions == 0
    assert await session.get(type(artifact), artifact.id) is None
    intent = await session.scalar(
        select(EventObjectCleanupIntent).where(
            EventObjectCleanupIntent.source_kind == ARTIFACT_RETENTION_CLEANUP_SOURCE,
            EventObjectCleanupIntent.source_key == str(artifact.id),
            EventObjectCleanupIntent.storage_path == artifact.storage_path,
        )
    )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.PENDING.value


async def test_retention_loop_retries_failed_unlink_on_next_cycle(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = FlakyCleanupStore(tmp_path / "artifacts")
    source_path = store.resolve(artifact.storage_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"retention-loop-retry")
    sleep_count = 0

    async def sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        artifact_worker,
        "ArtifactStore",
        lambda _root, namespace=None: store,
    )
    monkeypatch.setattr(artifact_worker.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await artifact_worker._run_retention_loop(
            session_factory=session_factory,
            configured=SimpleNamespace(
                artifact_retention_enabled=True,
                artifact_retention_interval_seconds=60,
                artifact_storage_root=str(tmp_path / "artifacts"),
            ),
        )

    assert store.calls == 2
    assert not source_path.exists()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == str(artifact.id)
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.COMPLETED.value


async def test_retention_loop_recovers_committed_intent_after_crash(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    source_path = store.resolve(artifact.storage_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"retention-crash-recovery")

    async with session_factory() as session:
        async with session.begin():
            await ArtifactRetentionService().retain_expired(
                session,
                observed_at=seeded_artifact_assessment.now,
                storage_namespace=store.storage_namespace,
            )

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        artifact_worker,
        "ArtifactStore",
        lambda _root, namespace=None: store,
    )
    monkeypatch.setattr(artifact_worker.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await artifact_worker._run_retention_loop(
            session_factory=session_factory,
            configured=SimpleNamespace(
                artifact_retention_enabled=True,
                artifact_retention_interval_seconds=60,
                artifact_storage_root=str(tmp_path / "artifacts"),
            ),
        )

    assert not source_path.exists()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == str(artifact.id)
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.COMPLETED.value


async def test_cleanup_recovery_runs_when_retention_disabled(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    event_id = uuid.uuid4()
    relative_path = "objects/aa/bb/candidate-recovery.txt"
    store = ArtifactStore(tmp_path / "artifacts")
    source_path = store.resolve(relative_path)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"candidate-recovery")

    async with session_factory() as session:
        async with session.begin():
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=uuid.uuid4().hex,
                storage_path=relative_path,
                storage_namespace=store.storage_namespace,
            )

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        artifact_worker,
        "ArtifactStore",
        lambda _root, namespace=None: store,
    )
    monkeypatch.setattr(artifact_worker.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await artifact_worker._run_retention_loop(
            session_factory=session_factory,
            configured=SimpleNamespace(
                artifact_retention_enabled=False,
                artifact_retention_interval_seconds=60,
                artifact_storage_root=str(tmp_path / "artifacts"),
            ),
        )

    assert not source_path.exists()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(EventObjectCleanupIntent.event_id == event_id)
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.COMPLETED.value


async def test_retention_loop_logs_all_modes_and_protected_counts(
    monkeypatch,
    caplog,
) -> None:
    result = ArtifactRetentionResult(
        test_deleted=1,
        drill_deleted=2,
        live_deleted=3,
        manual_deleted=4,
        replay_deleted=5,
        failed_deletions=6,
        protected_publication_count=7,
        protected_publication_reasons=(
            ("current_publication", 2),
            ("superseded_publication", 5),
        ),
    )
    sleep_count = 0

    class Service:
        async def retain_expired(self, session, *, observed_at, storage_namespace):
            del session, observed_at, storage_namespace
            return result

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

        def begin(self):
            return self

    async def sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            return
        raise asyncio.CancelledError

    async def process_cleanup_intents(*_args, **_kwargs):
        return SimpleNamespace(failed_count=0)

    monkeypatch.setattr(
        artifact_worker,
        "ArtifactRetentionService",
        lambda: Service(),
    )
    monkeypatch.setattr(
        artifact_worker,
        "process_cleanup_intents",
        process_cleanup_intents,
    )
    monkeypatch.setattr(artifact_worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger=artifact_worker.__name__):
        with pytest.raises(asyncio.CancelledError):
            await artifact_worker._run_retention_loop(
                session_factory=Session,
                configured=SimpleNamespace(
                    artifact_retention_enabled=True,
                    artifact_retention_interval_seconds=60,
                    artifact_storage_root="/tmp/artifact-retention-test",
                ),
            )

    message = caplog.records[-1].getMessage()
    assert '"test":1' in message
    assert '"drill":2' in message
    assert '"live":3' in message
    assert '"manual":4' in message
    assert '"replay":5' in message
    assert '"failed":6' in message
    assert '"protected_publications":7' in message
    assert '"current_publication":2' in message
    assert '"superseded_publication":5' in message
