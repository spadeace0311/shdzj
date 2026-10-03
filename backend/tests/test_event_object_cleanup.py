from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

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
    from app.db import engine

    await engine.dispose()
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
            )
            await enqueue_object_cleanup_intent(
                session,
                event_id=event_id,
                source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                source_key=uuid.uuid4().hex,
                storage_path=succeeding_path,
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
