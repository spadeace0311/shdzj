from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import delete, select

from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.event_object_cleanup import (
    ARTIFACT_RETENTION_CLEANUP_SOURCE,
    CleanupIntentStatus,
    EventObjectCleanupIntent,
    enqueue_object_cleanup_intent,
    process_cleanup_intents,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cleanup_tests():
    from app.db import SessionFactory, engine

    await engine.dispose()
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(EventObjectCleanupIntent))
    yield
    await engine.dispose()


class SelectiveFailureStore(ArtifactStore):
    def __init__(self, root: Path, *, failing_path: str) -> None:
        super().__init__(root)
        self._failing_path = failing_path

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        if stored.relative_path == self._failing_path:
            raise OSError("injected cleanup failure")
        super().delete_unreferenced(stored)


async def test_cleanup_intent_processor_reclaims_expired_processing_lease(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/cleanup-lease.txt"
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.resolve(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"lease-recovery")
    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventObjectCleanupIntent(
                    event_id=event_id,
                    source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                    source_key=source_key,
                    storage_path=relative_path,
                    storage_namespace=store.storage_namespace,
                    status=CleanupIntentStatus.PROCESSING.value,
                    attempt_count=1,
                    lease_owner="crashed-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                )
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="recovery-worker",
        observed_at=now,
    )

    assert result.failed_count == 0
    assert result.completed_count == 1
    assert not path.exists()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.COMPLETED.value
    assert intent.attempt_count == 2
    assert intent.lease_owner is None
    assert intent.lease_expires_at is None


async def test_cleanup_intent_processor_records_partial_failure(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    failing_path = "objects/aa/bb/failing.txt"
    succeeding_path = "objects/cc/dd/succeeding.txt"
    store = SelectiveFailureStore(
        tmp_path / "artifacts",
        failing_path=failing_path,
    )
    for relative_path in (failing_path, succeeding_path):
        path = store.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative_path.encode())

    async with session_factory() as session:
        async with session.begin():
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=uuid.uuid4().hex,
                storage_path=failing_path,
                storage_namespace=store.storage_namespace,
            )
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=uuid.uuid4().hex,
                storage_path=succeeding_path,
                storage_namespace=store.storage_namespace,
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="partial-failure-worker",
    )

    assert result.failed_count == 1
    assert result.completed_count == 1
    assert store.resolve(failing_path).is_file()
    assert not store.resolve(succeeding_path).exists()
    async with session_factory() as session:
        intents = (
            await session.scalars(
                select(EventObjectCleanupIntent)
                .where(EventObjectCleanupIntent.event_id == event_id)
                .order_by(EventObjectCleanupIntent.storage_path)
            )
        ).all()
    statuses = {intent.storage_path: intent.status for intent in intents}
    assert statuses[failing_path] == CleanupIntentStatus.PENDING.value
    assert statuses[succeeding_path] == CleanupIntentStatus.COMPLETED.value


async def test_cleanup_intent_processor_skips_cross_table_reference(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
) -> None:
    artifact = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
        production_mode="live",
    )
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.resolve(artifact.storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"referenced-object")
    source_key = uuid.uuid4().hex

    async with session_factory() as session:
        async with session.begin():
            await enqueue_object_cleanup_intent(
                session,
                event_id=artifact.event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=source_key,
                storage_path=artifact.storage_path,
                storage_namespace=store.storage_namespace,
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="reference-check-worker",
    )

    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert path.is_file()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.SKIPPED.value
    assert intent.completed_at is not None


async def test_cleanup_intent_namespace_prevents_wrong_root_delete(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/shared-relative-path.txt"
    store_a = ArtifactStore(tmp_path / "root-a")
    store_b = ArtifactStore(tmp_path / "root-b")
    path_a = store_a.resolve(relative_path)
    path_b = store_b.resolve(relative_path)
    for path, payload in ((path_a, b"root-a"), (path_b, b"root-b")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    async with session_factory() as session:
        async with session.begin():
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=source_key,
                storage_path=relative_path,
                storage_namespace=store_a.storage_namespace,
            )

    wrong_root = await process_cleanup_intents(
        session_factory,
        store_b,
        lease_owner="wrong-root-worker",
        source_key=source_key,
    )

    assert wrong_root.namespace_mismatch_count == 1
    assert wrong_root.succeeded is False
    assert wrong_root.completed_count == 0
    assert path_a.read_bytes() == b"root-a"
    assert path_b.read_bytes() == b"root-b"
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.PENDING.value
    assert intent.attempt_count == 0

    correct_root = await process_cleanup_intents(
        session_factory,
        store_a,
        lease_owner="correct-root-worker",
        source_key=source_key,
    )

    assert correct_root.completed_count == 1
    assert not path_a.exists()
    assert path_b.read_bytes() == b"root-b"


@pytest.mark.parametrize("namespace", ["", "   ", None])
async def test_enqueue_object_cleanup_intent_rejects_blank_storage_namespace(
    session_factory,
    namespace: object,
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="storage_namespace must not be empty"):
            await enqueue_object_cleanup_intent(
                session,
                event_id=uuid.uuid4(),
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=uuid.uuid4().hex,
                storage_path="objects/aa/bb/blank-namespace.txt",
                storage_namespace=cast(str, namespace),
            )


@pytest.mark.parametrize("namespace", ["", "   ", None])
async def test_cleanup_intent_processor_marks_blank_namespace_unprocessable(
    session_factory,
    tmp_path,
    namespace: object,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/blank-namespace.txt"
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.resolve(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"must-remain")

    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventObjectCleanupIntent(
                    event_id=event_id,
                    source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                    source_key=source_key,
                    storage_path=relative_path,
                    storage_namespace=cast(str | None, namespace),
                    status=CleanupIntentStatus.PENDING.value,
                    attempt_count=0,
                )
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="blank-namespace-worker",
        source_key=source_key,
    )

    assert result.succeeded is False
    assert result.namespace_mismatch_count == 1
    assert result.unprocessable_count == 1
    assert result.completed_count == 0
    assert result.skipped_count == 0
    assert path.read_bytes() == b"must-remain"
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.UNPROCESSABLE.value
    assert intent.completed_at is None
    assert intent.lease_owner is None
    assert intent.lease_expires_at is None
    assert intent.last_error == ("storage namespace unavailable; manual remediation required")


async def test_cleanup_intent_moves_to_dead_letter_after_max_attempts(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/dead-letter.txt"
    store = SelectiveFailureStore(
        tmp_path / "artifacts",
        failing_path=relative_path,
    )
    path = store.resolve(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dead-letter")

    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventObjectCleanupIntent(
                    event_id=event_id,
                    source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                    source_key=source_key,
                    storage_path=relative_path,
                    storage_namespace=store.storage_namespace,
                    status=CleanupIntentStatus.PENDING.value,
                    attempt_count=4,
                    max_attempts=5,
                )
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="dead-letter-worker",
    )

    assert result.dead_letter_count == 1
    assert result.failed_count == 0
    assert path.is_file()
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.status == CleanupIntentStatus.DEAD_LETTER.value
    assert intent.attempt_count == 5
    assert intent.lease_owner is None
    assert intent.lease_expires_at is None


async def test_cleanup_intent_processor_reports_preexisting_dead_letter_for_same_source(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/already-dead-letter.txt"
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.resolve(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dead-letter")

    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventObjectCleanupIntent(
                    event_id=event_id,
                    source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                    source_key=source_key,
                    storage_path=relative_path,
                    storage_namespace=store.storage_namespace,
                    status=CleanupIntentStatus.DEAD_LETTER.value,
                    attempt_count=5,
                    max_attempts=5,
                    last_error="cleanup attempt limit exceeded",
                )
            )

    result = await process_cleanup_intents(
        session_factory,
        store,
        lease_owner="dead-letter-replay-worker",
        source_key=source_key,
    )

    assert result.succeeded is False
    assert result.dead_letter_count == 1
    assert result.completed_count == 0
    assert path.read_bytes() == b"dead-letter"


async def test_cleanup_intent_error_redacts_absolute_storage_paths(
    session_factory,
    tmp_path,
) -> None:
    event_id = uuid.uuid4()
    source_key = uuid.uuid4().hex
    relative_path = "objects/aa/bb/redaction.txt"
    store = ArtifactStore(tmp_path / "artifacts")
    path = store.resolve(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"redaction")

    class AbsolutePathFailureStore(ArtifactStore):
        def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
            raise OSError(f"cannot unlink {stored.managed_path}")

    failing_store = AbsolutePathFailureStore(tmp_path / "artifacts")
    async with session_factory() as session:
        async with session.begin():
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=source_key,
                storage_path=relative_path,
                storage_namespace=failing_store.storage_namespace,
            )

    result = await process_cleanup_intents(
        session_factory,
        failing_store,
        lease_owner="redaction-worker",
    )

    assert result.failed_count == 1
    async with session_factory() as session:
        intent = await session.scalar(
            select(EventObjectCleanupIntent).where(
                EventObjectCleanupIntent.source_key == source_key
            )
        )
    assert intent is not None
    assert intent.last_error is not None
    assert str(tmp_path) not in intent.last_error
    assert "<artifact-object>" in intent.last_error
