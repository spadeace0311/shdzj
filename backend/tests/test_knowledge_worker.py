from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine.base import RootTransaction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.embedding.schemas import DENSE_DIMENSIONS, EmbeddingBatch
from app.knowledge.domain import KnowledgeJobStatus, KnowledgeVersionStatus
from app.knowledge.index import (
    IndexedChunk,
    KnowledgeIndex,
    KnowledgeIndexRebuild,
)
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
    KnowledgeWebSnapshot,
)
from app.knowledge.health import WorkerHealthState
from app.knowledge.publication import KnowledgePublicationService
from app.knowledge.storage import KnowledgeFileStore
from app.knowledge.worker import KnowledgeWorker, _build_index, _mark_success
from qdrant_client.models import Distance, SparseVectorParams, VectorParams


WORKER_ACTOR = "knowledge-worker-test"


def test_worker_health_state_expires_after_stale_window() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    state = WorkerHealthState(stale_seconds=30)

    state.mark_alive(now)

    assert state.payload(now) == {
        "status": "ok",
        "last_heartbeat_at": "2026-10-08T12:00:00+00:00",
    }
    assert state.payload(now + timedelta(seconds=31))["status"] == "unavailable"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_ingest_job_parses_chunks_and_indexes(session_factory, tmp_path: Path):
    try:
        store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024 * 1024)
        stored = await store.store_upload(
            BytesIO(b"Earthquake response plan\n\nEvacuation procedures."),
            file_name="preplan.txt",
            source_id="source",
            version="v1",
        )
        index = _FakeIndex()
        embeddings = _FakeEmbeddings()
        job_id, version_id = await _seed_uploaded_version_and_job(
            session_factory,
            stored,
        )

        worker = KnowledgeWorker(
            store=store,
            index=index,
            embeddings=embeddings,
        )

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            job = await session.get(KnowledgeJob, job_id)
            version = await session.get(KnowledgeSourceVersion, version_id)
            chunks = list(
                (
                    await session.scalars(
                        select(KnowledgeChunk).where(
                            KnowledgeChunk.version_id == version_id
                        )
                    )
                ).all()
            )
            assert job is not None
            assert job.status == KnowledgeJobStatus.SUCCEEDED.value
            assert version is not None
            assert version.status == KnowledgeVersionStatus.INDEXED.value
            assert len(chunks) >= 1
            assert index.upsert_count == 1
            assert embeddings.embed_count == 1
    finally:
        await _delete_actor_data(session_factory)


async def test_duplicate_successful_job_is_idempotent(
    session_factory,
    tmp_path: Path,
):
    try:
        store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024 * 1024)
        stored = await store.store_upload(
            BytesIO(b"Earthquake response plan\n\nEvacuation procedures."),
            file_name="preplan.txt",
            source_id="source",
            version="v1",
        )
        index = _FakeIndex()
        embeddings = _FakeEmbeddings()
        version_id = await _seed_uploaded_version_and_job(
            session_factory,
            stored,
            return_job_id=False,
        )
        await _seed_job(session_factory, version_id, "ingest")

        worker = KnowledgeWorker(
            store=store,
            index=index,
            embeddings=embeddings,
        )

        assert await _process_one(session_factory, worker) is True
        assert await _process_one(session_factory, worker) is True
        assert embeddings.embed_count == 1
        assert index.upsert_count == 1
    finally:
        await _delete_actor_data(session_factory)


async def test_force_index_job_rebuilds_published_version(
    session_factory,
):
    try:
        version_id, chunk_ids = await _seed_published_indexed_version(
            session_factory
        )
        expected_point_ids = {
            uuid5(NAMESPACE_URL, str(chunk_id)) for chunk_id in chunk_ids
        }
        index = _FakeIndex(
            points={
                point_id: version_id
                for point_id in expected_point_ids
            }
        )
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "restore empty collection"},
        )
        embeddings = _FakeEmbeddings()
        worker = KnowledgeWorker(index=index, embeddings=embeddings)

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            version = await session.get(KnowledgeSourceVersion, version_id)
            index_version = await session.scalar(
                select(KnowledgeIndexVersion).where(
                    KnowledgeIndexVersion.source_version_id == version_id
                )
            )
            assert version is not None
            assert index_version is not None
            assert version.status == KnowledgeVersionStatus.PUBLISHED.value
            assert index_version.status == KnowledgeVersionStatus.PUBLISHED.value
            assert index.deleted_version_ids == []
            assert index.upsert_count == 1
            assert index.upserted_point_ids == [
                uuid5(NAMESPACE_URL, str(chunk_id)) for chunk_id in chunk_ids
            ]
            assert index.retrieve(version_id) == expected_point_ids
            assert embeddings.embed_count == 1
    finally:
        await _delete_actor_data(session_factory)


@pytest.mark.parametrize("failure_stage", ["embedding", "upsert"])
async def test_force_rebuild_failure_keeps_published_points_retrievable(
    session_factory,
    failure_stage: str,
):
    try:
        version_id, chunk_ids = await _seed_published_indexed_version(
            session_factory
        )
        old_points = {
            uuid5(NAMESPACE_URL, str(chunk_id)): version_id
            for chunk_id in chunk_ids
        }
        index = _FakeIndex(
            points=old_points,
            fail_upsert=failure_stage == "upsert",
        )
        embeddings = (
            _FailingEmbeddings()
            if failure_stage == "embedding"
            else _FakeEmbeddings()
        )
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "failure safety"},
        )
        worker = KnowledgeWorker(index=index, embeddings=embeddings)

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            version = await session.get(KnowledgeSourceVersion, version_id)
            assert version is not None
            assert version.status == KnowledgeVersionStatus.PUBLISHED.value
        assert index.deleted_version_ids == []
        assert index.retrieve(version_id) == set(old_points)
        assert index.points == old_points
    finally:
        await _delete_actor_data(session_factory)


@pytest.mark.parametrize("failure_stage", ["embedding", "upsert"])
async def test_force_rebuild_exhaustion_keeps_published_version_healthy(
    session_factory,
    failure_stage: str,
):
    try:
        version_id, chunk_ids = await _seed_published_indexed_version(
            session_factory
        )
        old_points = {
            uuid5(NAMESPACE_URL, str(chunk_id)): version_id
            for chunk_id in chunk_ids
        }
        index = _FakeIndex(
            points=old_points,
            fail_upsert=failure_stage == "upsert",
        )
        embeddings = (
            _FailingEmbeddings()
            if failure_stage == "embedding"
            else _FakeEmbeddings()
        )
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "retry exhaustion"},
            max_attempts=2,
        )
        current_time = [datetime.now(UTC)]
        worker = KnowledgeWorker(
            index=index,
            embeddings=embeddings,
            now=lambda: current_time[0],
        )

        assert await _process_one(session_factory, worker) is True
        async with session_factory() as session:
            job = await session.scalar(
                select(KnowledgeJob).where(
                    KnowledgeJob.version_id == version_id,
                    KnowledgeJob.request_payload["force"].as_boolean(),
                )
            )
            version = await session.get(KnowledgeSourceVersion, version_id)
            assert job is not None
            assert job.status == KnowledgeJobStatus.QUEUED.value
            assert job.attempt_count == 1
            assert version is not None
            assert version.status == KnowledgeVersionStatus.PUBLISHED.value
        assert index.retrieve(version_id) == set(old_points)

        current_time[0] += timedelta(seconds=3)
        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            job = await session.scalar(
                select(KnowledgeJob).where(
                    KnowledgeJob.version_id == version_id,
                    KnowledgeJob.request_payload["force"].as_boolean(),
                )
            )
            version = await session.get(KnowledgeSourceVersion, version_id)
            index_version = await session.scalar(
                select(KnowledgeIndexVersion).where(
                    KnowledgeIndexVersion.source_version_id == version_id
                )
            )
            assert job is not None
            assert job.status == KnowledgeJobStatus.DEAD_LETTER.value
            assert job.attempt_count == 2
            assert version is not None
            assert version.status == KnowledgeVersionStatus.PUBLISHED.value
            assert index_version is not None
            assert index_version.status == KnowledgeVersionStatus.PUBLISHED.value
        assert index.deleted_version_ids == []
        assert index.retrieve(version_id) == set(old_points)
        assert index.points == old_points
    finally:
        await _delete_actor_data(session_factory)


async def test_force_rebuild_embeds_and_upserts_in_bounded_batches(
    session_factory,
) -> None:
    try:
        version_id, chunk_ids = await _seed_chunked_published_version(
            session_factory,
            chunk_count=25,
        )
        index = _BatchingIndex(
            points={
                uuid5(NAMESPACE_URL, str(chunk_id)): version_id
                for chunk_id in chunk_ids
            }
        )
        embeddings = _BatchingEmbeddings()
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "bounded rebuild"},
        )
        worker = KnowledgeWorker(
            index=index,
            embeddings=embeddings,
            batch_size=10,
        )

        assert await _process_one(session_factory, worker) is True

        assert embeddings.sizes == [10, 10, 5]
        assert index.upsert_sizes == [10, 10, 5]
        assert index.commit_count == 1
        assert len(index.retrieve(version_id)) == 25
    finally:
        await _delete_actor_data(session_factory)


async def test_late_batch_upsert_failure_preserves_old_points(
    session_factory,
) -> None:
    try:
        version_id, chunk_ids = await _seed_chunked_published_version(
            session_factory,
            chunk_count=25,
        )
        old_points = {
            uuid5(NAMESPACE_URL, str(chunk_id)): version_id
            for chunk_id in chunk_ids
        }
        index = _BatchingIndex(
            points=old_points,
            fail_upsert_call=2,
        )
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "late batch failure"},
            max_attempts=2,
        )
        worker = KnowledgeWorker(
            index=index,
            embeddings=_BatchingEmbeddings(),
            batch_size=10,
        )

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            version = await session.get(KnowledgeSourceVersion, version_id)
            index_version = await session.scalar(
                select(KnowledgeIndexVersion).where(
                    KnowledgeIndexVersion.source_version_id == version_id
                )
            )
            assert version is not None
            assert index_version is not None
            assert version.status == KnowledgeVersionStatus.PUBLISHED.value
            assert index_version.status == KnowledgeVersionStatus.PUBLISHED.value
        assert index.upsert_sizes == [10, 10]
        assert index.commit_count == 0
        assert index.abort_count == 1
        assert index.points == old_points
    finally:
        await _delete_actor_data(session_factory)


async def _seeded_real_index(
    session_factory,
) -> tuple[UUID, list[UUID], _AliasQdrantClient, str, KnowledgeIndex]:
    version_id, chunk_ids = await _seed_published_indexed_version(
        session_factory
    )
    async with session_factory() as session:
        index_version = await session.scalar(
            select(KnowledgeIndexVersion).where(
                KnowledgeIndexVersion.source_version_id == version_id
            )
        )
        assert index_version is not None
        logical_name = index_version.collection_name
    client = _AliasQdrantClient()
    client.collections.add(logical_name)
    client.collection_infos[logical_name] = _alias_collection_info()
    index = KnowledgeIndex(client=client)
    return version_id, chunk_ids, client, logical_name, index


async def _build_index_with_failing_flush(
    session_factory,
    version_id: UUID,
    index: KnowledgeIndex,
) -> None:
    async with session_factory() as session:
        version = await session.get(KnowledgeSourceVersion, version_id)
        source = await session.get(KnowledgeSource, version.source_id)
        assert version is not None
        assert source is not None
        original_flush = session.flush
        flush_calls = 0

        async def fail_after_activation() -> None:
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 2:
                raise RuntimeError("post-activation flush failed")
            await original_flush()

        session.flush = fail_after_activation
        with pytest.raises(RuntimeError, match="post-activation flush failed"):
            await _build_index(
                session,
                index,
                _FakeEmbeddings(),
                version,
                source,
                2,
                force_rebuild=True,
                batch_size=10,
            )


async def test_real_index_restores_previous_alias_after_post_activation_failure(
    session_factory,
) -> None:
    try:
        version_id, _chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"
        client.aliases[alias_name] = logical_name

        await _build_index_with_failing_flush(
            session_factory,
            version_id,
            index,
        )

        assert client.aliases.get(alias_name) == logical_name
        assert not any(
            collection.startswith(f"{logical_name}-")
            and collection != logical_name
            for collection in client.collections
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_removes_new_alias_after_post_activation_failure(
    session_factory,
) -> None:
    try:
        version_id, _chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"

        await _build_index_with_failing_flush(
            session_factory,
            version_id,
            index,
        )

        assert alias_name not in client.aliases
        assert not any(
            collection.startswith(f"{logical_name}-")
            and collection != logical_name
            for collection in client.collections
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_success_leaves_new_alias_active(
    session_factory,
) -> None:
    try:
        version_id, chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "real alias success"},
        )
        worker = KnowledgeWorker(index=index, embeddings=_FakeEmbeddings())

        assert await _process_one(session_factory, worker) is True

        assert client.aliases.get(alias_name) is not None
        assert client.aliases[alias_name] != logical_name
        assert client.aliases[alias_name] in client.collections
        assert len(client.aliases) == 1
        assert len(
            {
                collection
                for collection in client.collections
                if collection != logical_name
            }
        ) == 1
        del chunk_ids
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_compensates_outer_commit_failure(
    session_factory,
    monkeypatch,
) -> None:
    try:
        version_id, _chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"
        client.aliases[alias_name] = logical_name
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "outer commit failure"},
        )
        worker = KnowledgeWorker(index=index, embeddings=_FakeEmbeddings())

        def fail_root_commit(_transaction: object) -> None:
            raise RuntimeError("outer commit failed")

        monkeypatch.setattr(RootTransaction, "commit", fail_root_commit)
        async with session_factory() as session:
            with pytest.raises(RuntimeError, match="outer commit failed"):
                async with session.begin():
                    await worker.process_one(session)
            await worker.compensate_pending_alias_rebuild()
        monkeypatch.undo()

        assert client.aliases.get(alias_name) == logical_name
        assert not any(
            collection.startswith(f"{logical_name}-")
            and collection != logical_name
            for collection in client.collections
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_compensates_cancellation_after_alias_activation(
    session_factory,
) -> None:
    try:
        version_id, _chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"
        client.aliases[alias_name] = logical_name

        async with session_factory() as session:
            version = await session.get(KnowledgeSourceVersion, version_id)
            source = await session.get(KnowledgeSource, version.source_id)
            assert version is not None
            assert source is not None
            original_flush = session.flush
            flush_calls = 0

            async def cancel_after_activation() -> None:
                nonlocal flush_calls
                flush_calls += 1
                if flush_calls == 2:
                    raise asyncio.CancelledError
                await original_flush()

            session.flush = cancel_after_activation
            with pytest.raises(asyncio.CancelledError):
                await _build_index(
                    session,
                    index,
                    _FakeEmbeddings(),
                    version,
                    source,
                    2,
                    force_rebuild=True,
                    batch_size=10,
                )

        assert client.aliases.get(alias_name) == logical_name
        assert not any(
            collection.startswith(f"{logical_name}-")
            and collection != logical_name
            for collection in client.collections
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_compensates_when_mark_success_fails(
    session_factory,
    monkeypatch,
) -> None:
    try:
        version_id, _chunk_ids, client, logical_name, index = (
            await _seeded_real_index(session_factory)
        )
        alias_name = f"{logical_name}-active"
        client.aliases[alias_name] = logical_name
        await _seed_job(
            session_factory,
            version_id,
            "index",
            payload={"force": True, "reason": "mark success failure"},
        )
        worker = KnowledgeWorker(index=index, embeddings=_FakeEmbeddings())

        async def fail_mark_success(
            session: AsyncSession,
            job_id: UUID,
            now: datetime,
        ) -> None:
            await _mark_success(session, job_id, now)
            raise RuntimeError("mark success failed")

        monkeypatch.setattr(
            "app.knowledge.worker._mark_success",
            fail_mark_success,
        )
        async with session_factory() as session:
            async with session.begin():
                assert await worker.process_one(session) is True
        monkeypatch.undo()

        assert client.aliases.get(alias_name) == logical_name
        assert not any(
            collection.startswith(f"{logical_name}-")
            and collection != logical_name
            for collection in client.collections
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_real_index_compensation_is_state_aware_and_idempotent() -> None:
    client = _AliasQdrantClient()
    index = KnowledgeIndex(client=client)
    rebuild = KnowledgeIndexRebuild(
        logical_name="logical",
        alias_name="logical-active",
        staging_name="logical-staging",
        previous_name="logical",
        previous_alias_exists=True,
    )

    client.aliases[rebuild.alias_name] = rebuild.previous_name or ""
    await index.compensate_rebuild(rebuild)
    await index.compensate_rebuild(rebuild)

    assert client.aliases[rebuild.alias_name] == "logical"
    assert client.alias_operations == []

    no_previous = KnowledgeIndexRebuild(
        logical_name="logical",
        alias_name="logical-active",
        staging_name="logical-staging",
        previous_name=None,
        previous_alias_exists=False,
    )
    client.aliases.clear()
    await index.compensate_rebuild(no_previous)
    await index.compensate_rebuild(no_previous)

    assert no_previous.alias_name not in client.aliases
    assert client.alias_operations == []


@pytest.mark.parametrize("job_type", ["ingest", "index"])
async def test_worker_dead_letters_after_attempt_budget(
    session_factory,
    tmp_path: Path,
    job_type: str,
):
    try:
        version_id = await _seed_version_and_job(
            session_factory,
            status=KnowledgeVersionStatus.UPLOADED.value,
            job_type=job_type,
            attempt_count=1,
            max_attempts=2,
        )
        worker = KnowledgeWorker(
            store=KnowledgeFileStore(tmp_path, max_upload_bytes=1024),
            index=_FakeIndex(),
            embeddings=_FakeEmbeddings(),
        )

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            job = await session.scalar(
                select(KnowledgeJob).where(KnowledgeJob.version_id == version_id)
            )
            version = await session.get(KnowledgeSourceVersion, version_id)
            assert job is not None
            assert job.status == KnowledgeJobStatus.DEAD_LETTER.value
            assert version is not None
            assert version.status == KnowledgeVersionStatus.FAILED.value
    finally:
        await _delete_actor_data(session_factory)


async def test_worker_requeues_failed_job_with_backoff(
    session_factory,
    tmp_path: Path,
):
    try:
        version_id = await _seed_version_and_job(
            session_factory,
            status=KnowledgeVersionStatus.UPLOADED.value,
            job_type="ingest",
            attempt_count=0,
            max_attempts=2,
        )
        worker = KnowledgeWorker(
            store=KnowledgeFileStore(tmp_path, max_upload_bytes=1024),
            index=_FakeIndex(),
            embeddings=_FakeEmbeddings(),
        )

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            job = await session.scalar(
                select(KnowledgeJob).where(KnowledgeJob.version_id == version_id)
            )
            assert job is not None
            assert job.status == KnowledgeJobStatus.QUEUED.value
            assert job.attempt_count == 1
            assert job.lease_expires_at is None
            assert job.last_error is not None
    finally:
        await _delete_actor_data(session_factory)


async def test_disabled_version_worker_job_does_not_change_status(
    session_factory,
    tmp_path: Path,
) -> None:
    try:
        version_id = await _seed_version_and_job(
            session_factory,
            status=KnowledgeVersionStatus.DISABLED.value,
            job_type="ingest",
            attempt_count=0,
            max_attempts=1,
        )
        worker = KnowledgeWorker(
            store=KnowledgeFileStore(tmp_path, max_upload_bytes=1024),
            index=_FakeIndex(),
            embeddings=_FakeEmbeddings(),
        )

        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            job = await session.scalar(
                select(KnowledgeJob).where(
                    KnowledgeJob.version_id == version_id
                )
            )
            version = await session.get(KnowledgeSourceVersion, version_id)
            assert job is not None
            assert job.status == KnowledgeJobStatus.DEAD_LETTER.value
            assert version is not None
            assert version.status == KnowledgeVersionStatus.DISABLED.value
            assert version.failure_reason is None
    finally:
        await _delete_actor_data(session_factory)


async def test_worker_publish_and_rollback_use_publication_service(
    session_factory,
):
    try:
        previous_id, current_id = await _seed_publishable_versions(session_factory)
        await _seed_job(
            session_factory,
            current_id,
            "publish",
            payload={"actor": WORKER_ACTOR, "reason": "release current"},
        )

        worker = KnowledgeWorker(
            publication=KnowledgePublicationService(),
            index=_FakeIndex(),
            embeddings=_FakeEmbeddings(),
        )
        assert await _process_one(session_factory, worker) is True

        await _seed_job(
            session_factory,
            previous_id,
            "rollback",
            payload={"actor": WORKER_ACTOR, "reason": "rollback previous"},
        )
        assert await _process_one(session_factory, worker) is True

        async with session_factory() as session:
            current = await session.get(KnowledgeSourceVersion, current_id)
            previous = await session.get(KnowledgeSourceVersion, previous_id)
            assert current is not None
            assert previous is not None
            assert current.status == KnowledgeVersionStatus.INDEXED.value
            assert previous.status == KnowledgeVersionStatus.PUBLISHED.value
    finally:
        await _delete_actor_data(session_factory)


async def test_lifecycle_jobs_execute_even_after_prior_success(
    session_factory,
):
    try:
        previous_id, current_id = await _seed_publishable_versions(session_factory)
        worker = KnowledgeWorker(
            publication=KnowledgePublicationService(),
            index=_FakeIndex(),
            embeddings=_FakeEmbeddings(),
        )

        async def run(version_id: UUID, job_type: str, reason: str) -> None:
            await _seed_job(
                session_factory,
                version_id,
                job_type,
                payload={"actor": WORKER_ACTOR, "reason": reason},
            )
            assert await _process_one(session_factory, worker) is True

        await run(previous_id, "publish", "release v1")
        await run(current_id, "publish", "release v2")
        await run(previous_id, "rollback", "rollback v1")
        await run(previous_id, "publish", "publish v1 again")

        async with session_factory() as session:
            previous = await session.get(KnowledgeSourceVersion, previous_id)
            current = await session.get(KnowledgeSourceVersion, current_id)
            final_publish = await session.scalar(
                select(KnowledgeJob)
                .where(
                    KnowledgeJob.version_id == previous_id,
                    KnowledgeJob.job_type == "publish",
                )
                .order_by(
                    KnowledgeJob.created_at.desc(),
                    KnowledgeJob.id.desc(),
                )
                .limit(1)
            )
            assert previous is not None
            assert current is not None
            assert previous.status == KnowledgeVersionStatus.PUBLISHED.value
            assert current.status == KnowledgeVersionStatus.INDEXED.value
            assert final_publish is not None
            assert final_publish.status == KnowledgeJobStatus.SUCCEEDED.value
            assert final_publish.result_payload == {
                "status": "already_published",
                "version_id": str(previous_id),
            }
    finally:
        await _delete_actor_data(session_factory)


async def test_online_refresh_scan_enqueues_one_due_version_per_interval(
    session_factory,
) -> None:
    source_key = f"worker.refresh.{uuid4()}"
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    source_id = await _seed_refresh_source(
        session_factory,
        source_key=source_key,
        fetched_at=now - timedelta(days=2),
    )
    worker = KnowledgeWorker(
        index=_FakeIndex(),
        embeddings=_FakeEmbeddings(),
        now=lambda: now,
        refresh_interval_seconds=86_400,
        online_search_enabled=True,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                first = await worker.enqueue_due_refreshes(session)
                second = await worker.enqueue_due_refreshes(session)

        assert first == 1
        assert second == 0
        async with session_factory() as session:
            versions = list(
                (
                    await session.scalars(
                        select(KnowledgeSourceVersion)
                        .where(KnowledgeSourceVersion.source_id == source_id)
                        .order_by(KnowledgeSourceVersion.created_at)
                    )
                ).all()
            )
            refresh_jobs = list(
                (
                    await session.scalars(
                        select(KnowledgeJob).where(
                            KnowledgeJob.version_id.in_(
                                version.id for version in versions
                            ),
                            KnowledgeJob.job_type == "fetch",
                            KnowledgeJob.request_payload["refresh"].as_boolean(),
                        )
                    )
                ).all()
            )
        assert len(versions) == 2
        assert versions[1].version.startswith("auto-refresh-")
        assert versions[1].status == KnowledgeVersionStatus.REGISTERED.value
        assert len(refresh_jobs) == 1
    finally:
        await _delete_refresh_source(session_factory, source_id)


async def test_online_refresh_scan_is_disabled_when_online_search_is_off(
    session_factory,
) -> None:
    source_key = f"worker.refresh-offline.{uuid4()}"
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    source_id = await _seed_refresh_source(
        session_factory,
        source_key=source_key,
        fetched_at=now - timedelta(days=2),
    )
    worker = KnowledgeWorker(
        index=_FakeIndex(),
        embeddings=_FakeEmbeddings(),
        now=lambda: now,
        refresh_interval_seconds=86_400,
        online_search_enabled=False,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                count = await worker.enqueue_due_refreshes(session)

        assert count == 0
        async with session_factory() as session:
            versions = list(
                (
                    await session.scalars(
                        select(KnowledgeSourceVersion).where(
                            KnowledgeSourceVersion.source_id == source_id
                        )
                    )
                ).all()
            )
        assert len(versions) == 1
    finally:
        await _delete_refresh_source(session_factory, source_id)


async def _process_one(session_factory, worker: KnowledgeWorker) -> bool:
    async with session_factory() as session:
        try:
            async with session.begin():
                result = await worker.process_one(session)
        except BaseException:
            await worker.compensate_pending_alias_rebuild()
            raise
        worker.clear_pending_alias_rebuild()
        return result


class _AliasQdrantClient:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.collection_infos: dict[str, object] = {}
        self.aliases: dict[str, str] = {}
        self.alias_operations: list[list[object]] = []
        self.deleted_collections: list[str] = []

    async def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    async def get_collection(self, collection_name: str, **kwargs: object) -> object:
        del kwargs
        return self.collection_infos[collection_name]

    async def create_collection(
        self,
        collection_name: str,
        **kwargs: object,
    ) -> bool:
        del kwargs
        self.collections.add(collection_name)
        self.collection_infos[collection_name] = _alias_collection_info()
        return True

    async def upsert(
        self,
        collection_name: str,
        points: list,
        **kwargs: object,
    ) -> None:
        del collection_name, points, kwargs

    async def scroll(
        self,
        collection_name: str,
        *,
        limit: int = 10,
        with_payload: bool = True,
        **kwargs: object,
    ) -> tuple[list, object]:
        del collection_name, limit, with_payload, kwargs
        return [], None

    async def update_collection_aliases(self, operations: list[object]) -> None:
        self.alias_operations.append(operations)
        for operation in operations:
            delete_alias = getattr(operation, "delete_alias", None)
            if delete_alias is not None:
                self.aliases.pop(getattr(delete_alias, "alias_name"), None)
                continue
            create_alias = getattr(operation, "create_alias", None)
            if create_alias is not None:
                self.aliases[getattr(create_alias, "alias_name")] = getattr(
                    create_alias,
                    "collection_name",
                )

    async def get_aliases(self) -> object:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias_name, collection_name=collection_name)
                for alias_name, collection_name in self.aliases.items()
            ]
        )

    async def delete_collection(self, collection_name: str) -> None:
        self.collections.discard(collection_name)
        self.collection_infos.pop(collection_name, None)
        self.deleted_collections.append(collection_name)
        self.aliases = {
            alias_name: target
            for alias_name, target in self.aliases.items()
            if target != collection_name
        }

    async def close(self) -> None:
        return None


def _alias_collection_info() -> object:
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors={
                    "dense": VectorParams(
                        size=1024,
                        distance=Distance.COSINE,
                    )
                },
                sparse_vectors={"sparse": SparseVectorParams()},
            )
        ),
        payload_schema={},
    )


class _FakeIndex:
    def __init__(
        self,
        *,
        points: dict[UUID, UUID] | None = None,
        fail_upsert: bool = False,
    ) -> None:
        self.deleted_version_ids: list[UUID] = []
        self.upsert_count = 0
        self.upserted_point_ids: list[UUID] = []
        self.points = dict(points or {})
        self.fail_upsert = fail_upsert
        self._staging_points: dict[UUID, UUID] = {}
        self._build: SimpleNamespace | None = None

    async def ensure_collection(self, index_version: KnowledgeIndexVersion) -> None:
        del index_version

    async def prepare_rebuild(
        self,
        index_version: KnowledgeIndexVersion,
    ) -> SimpleNamespace:
        self._build = SimpleNamespace(
            logical_name=index_version.collection_name,
            alias_name=f"{index_version.collection_name}-active",
            staging_name=f"{index_version.collection_name}-staging",
            previous_name=index_version.collection_name,
            previous_alias_exists=False,
        )
        self._staging_points = {}
        return self._build

    async def commit_rebuild(self, build: SimpleNamespace) -> None:
        del build
        self.points = dict(self._staging_points)
        self._staging_points = {}
        self._build = None

    async def abort_rebuild(self, build: SimpleNamespace) -> None:
        del build
        self._staging_points = {}
        self._build = None

    async def delete_version(
        self,
        index_version: KnowledgeIndexVersion,
        version_id: UUID,
    ) -> None:
        del index_version
        self.deleted_version_ids.append(version_id)
        self.points = {
            point_id: stored_version_id
            for point_id, stored_version_id in self.points.items()
            if stored_version_id != version_id
        }

    async def upsert_chunks(
        self,
        index_version: KnowledgeIndexVersion,
        chunks: list[IndexedChunk],
        embeddings: EmbeddingBatch,
        *,
        collection_name: str | None = None,
    ) -> None:
        del index_version, embeddings
        self.upsert_count += 1
        self.upserted_point_ids = [
            uuid5(NAMESPACE_URL, str(chunk.chunk_id)) for chunk in chunks
        ]
        if self.fail_upsert:
            raise RuntimeError("qdrant upsert failed")
        target = self.points
        if (
            self._build is not None
            and collection_name == self._build.staging_name
        ):
            target = self._staging_points
        for point_id, chunk in zip(
            self.upserted_point_ids,
            chunks,
            strict=True,
        ):
            target[point_id] = chunk.version_id

    def retrieve(self, version_id: UUID) -> set[UUID]:
        return {
            point_id
            for point_id, stored_version_id in self.points.items()
            if stored_version_id == version_id
        }


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.embed_count = 0

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.embed_count += 1
        return EmbeddingBatch(
            dense=[[0.1] * DENSE_DIMENSIONS for _ in texts],
            sparse=[{} for _ in texts],
        )


class _BatchingEmbeddings:
    def __init__(self) -> None:
        self.sizes: list[int] = []

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.sizes.append(len(texts))
        return EmbeddingBatch(
            dense=[[0.1] * DENSE_DIMENSIONS for _ in texts],
            sparse=[{} for _ in texts],
        )


class _BatchingIndex:
    def __init__(
        self,
        *,
        points: dict[UUID, UUID] | None = None,
        fail_upsert_call: int | None = None,
    ) -> None:
        self.points = dict(points or {})
        self.fail_upsert_call = fail_upsert_call
        self.upsert_sizes: list[int] = []
        self.commit_count = 0
        self.abort_count = 0
        self._staging: dict[UUID, UUID] = {}
        self._build: SimpleNamespace | None = None

    async def ensure_collection(
        self,
        index_version: KnowledgeIndexVersion,
    ) -> None:
        del index_version

    async def prepare_rebuild(
        self,
        index_version: KnowledgeIndexVersion,
    ) -> SimpleNamespace:
        self._build = SimpleNamespace(
            logical_name=index_version.collection_name,
            alias_name=f"{index_version.collection_name}-active",
            staging_name=f"{index_version.collection_name}-staging",
            previous_name=index_version.collection_name,
            previous_alias_exists=False,
        )
        self._staging = {}
        return self._build

    async def upsert_chunks(
        self,
        index_version: KnowledgeIndexVersion,
        chunks: list[IndexedChunk],
        embeddings: EmbeddingBatch,
        *,
        collection_name: str | None = None,
    ) -> None:
        del index_version, embeddings
        self.upsert_sizes.append(len(chunks))
        if (
            self.fail_upsert_call is not None
            and len(self.upsert_sizes) == self.fail_upsert_call
        ):
            raise RuntimeError("qdrant late batch upsert failed")
        target = self._staging if self._build is not None else self.points
        del collection_name
        for chunk in chunks:
            target[uuid5(NAMESPACE_URL, str(chunk.chunk_id))] = chunk.version_id

    async def commit_rebuild(self, build: SimpleNamespace) -> None:
        del build
        self.commit_count += 1
        self.points = dict(self._staging)

    async def abort_rebuild(self, build: SimpleNamespace) -> None:
        del build
        self.abort_count += 1
        self._staging = {}

    def retrieve(self, version_id: UUID) -> set[UUID]:
        return {
            point_id
            for point_id, stored_version_id in self.points.items()
            if stored_version_id == version_id
        }


class _FailingEmbeddings:
    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        del texts
        raise RuntimeError("embedding unavailable")


async def _seed_uploaded_version_and_job(
    session_factory,
    stored,
    *,
    return_job_id: bool = True,
):
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"worker.ingest.{uuid4()}",
                title="Worker Ingest Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status=KnowledgeVersionStatus.UPLOADED.value,
                file_name=stored.file_name,
                mime_type="text/plain",
                storage_path=stored.managed_path,
                checksum=stored.checksum,
                size_bytes=stored.size_bytes,
                created_by=WORKER_ACTOR,
            )
            session.add(version)
            await session.flush()
            job = KnowledgeJob(
                version_id=version.id,
                job_type="ingest",
                status=KnowledgeJobStatus.QUEUED.value,
                max_attempts=settings.knowledge_job_max_attempts,
                request_payload={},
            )
            session.add(job)
            await session.flush()
            if return_job_id:
                return job.id, version.id
            return version.id


async def _seed_version_and_job(
    session_factory,
    *,
    status: str,
    job_type: str,
    attempt_count: int,
    max_attempts: int,
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"worker.retry.{uuid4()}",
                title="Worker Retry Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status=status,
                file_name="missing.txt",
                mime_type="text/plain",
                storage_path="/definitely/missing/missing.txt",
                created_by=WORKER_ACTOR,
            )
            session.add(version)
            await session.flush()
            job = KnowledgeJob(
                version_id=version.id,
                job_type=job_type,
                status=KnowledgeJobStatus.QUEUED.value,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
            )
            session.add(job)
            await session.flush()
            return version.id


async def _seed_published_indexed_version(
    session_factory,
) -> tuple[UUID, list[UUID]]:
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"worker.rebuild.{uuid4()}",
                title="Worker Rebuild Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status=KnowledgeVersionStatus.PUBLISHED.value,
                created_by=WORKER_ACTOR,
            )
            session.add(version)
            await session.flush()
            chunk_ids = [uuid4(), uuid4()]
            index_version = KnowledgeIndexVersion(
                source_version_id=version.id,
                version="v1",
                status=KnowledgeVersionStatus.PUBLISHED.value,
                collection_name=(
                    f"{settings.qdrant_collection_prefix}-{source.source_key}"
                ),
                embedding_model=settings.embedding_model_name,
                reranker_model=settings.reranker_model_name,
                chunk_count=len(chunk_ids),
            )
            session.add(index_version)
            await session.flush()
            session.add_all(
                [
                    KnowledgeChunk(
                        id=chunk_id,
                        version_id=version.id,
                        chunk_no=chunk_no,
                        section_path=["重建"],
                        checksum=str(chunk_no) * 64,
                        search_text=f"上海市活动断层距离测试 {chunk_no}",
                        text=f"上海市活动断层距离测试 {chunk_no}",
                    )
                    for chunk_no, chunk_id in enumerate(chunk_ids, start=1)
                ]
            )
            session.add(
                KnowledgeJob(
                    version_id=version.id,
                    job_type="index",
                    status=KnowledgeJobStatus.SUCCEEDED.value,
                    max_attempts=settings.knowledge_job_max_attempts,
                    request_payload={},
                )
            )
            await session.flush()
            return version.id, chunk_ids


async def _seed_chunked_published_version(
    session_factory,
    *,
    chunk_count: int,
) -> tuple[UUID, list[UUID]]:
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"worker.batch.{uuid4()}",
                title="Worker Batch Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status=KnowledgeVersionStatus.PUBLISHED.value,
                created_by=WORKER_ACTOR,
            )
            session.add(version)
            await session.flush()
            chunk_ids = [uuid4() for _ in range(chunk_count)]
            index_version = KnowledgeIndexVersion(
                source_version_id=version.id,
                version="v1",
                status=KnowledgeVersionStatus.PUBLISHED.value,
                collection_name=(
                    f"{settings.qdrant_collection_prefix}-{source.source_key}"
                ),
                embedding_model=settings.embedding_model_name,
                reranker_model=settings.reranker_model_name,
                chunk_count=chunk_count,
            )
            session.add(index_version)
            await session.flush()
            session.add_all(
                [
                    KnowledgeChunk(
                        id=chunk_id,
                        version_id=version.id,
                        chunk_no=chunk_no,
                        section_path=["批量"],
                        checksum=str(chunk_no).zfill(64),
                        search_text=f"batch chunk {chunk_no}",
                        text=f"batch chunk {chunk_no}",
                    )
                    for chunk_no, chunk_id in enumerate(
                        chunk_ids,
                        start=1,
                    )
                ]
            )
            session.add(
                KnowledgeJob(
                    version_id=version.id,
                    job_type="index",
                    status=KnowledgeJobStatus.SUCCEEDED.value,
                    max_attempts=settings.knowledge_job_max_attempts,
                    request_payload={},
                )
            )
            await session.flush()
            return version.id, chunk_ids


async def _seed_publishable_versions(session_factory):
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"worker.publish.{uuid4()}",
                title="Worker Publish Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            previous = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status=KnowledgeVersionStatus.INDEXED.value,
                checksum="a" * 64,
                created_by=WORKER_ACTOR,
            )
            current = KnowledgeSourceVersion(
                source_id=source.id,
                version="v2",
                status=KnowledgeVersionStatus.INDEXED.value,
                checksum="b" * 64,
                created_by=WORKER_ACTOR,
            )
            session.add_all([previous, current])
            await session.flush()
            session.add_all(
                [
                    KnowledgeIndexVersion(
                        source_version_id=previous.id,
                        version="v1",
                        status=KnowledgeVersionStatus.INDEXED.value,
                        collection_name=(
                            f"{settings.qdrant_collection_prefix}-{source.source_key}"
                        ),
                        embedding_model=settings.embedding_model_name,
                        reranker_model=settings.reranker_model_name,
                    ),
                    KnowledgeIndexVersion(
                        source_version_id=current.id,
                        version="v2",
                        status=KnowledgeVersionStatus.INDEXED.value,
                        collection_name=(
                            f"{settings.qdrant_collection_prefix}-{source.source_key}"
                        ),
                        embedding_model=settings.embedding_model_name,
                        reranker_model=settings.reranker_model_name,
                    ),
                ]
            )
            await session.flush()
            return previous.id, current.id


async def _seed_job(
    session_factory,
    version_id: UUID,
    job_type: str,
    *,
    payload: dict | None = None,
    max_attempts: int | None = None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = KnowledgeJob(
                version_id=version_id,
                job_type=job_type,
                status=KnowledgeJobStatus.QUEUED.value,
                max_attempts=(
                    max_attempts
                    if max_attempts is not None
                    else settings.knowledge_job_max_attempts
                ),
                request_payload=payload or {},
            )
            session.add(job)
            await session.flush()


async def _seed_refresh_source(
    session_factory,
    *,
    source_key: str,
    fetched_at: datetime,
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=source_key,
                title="Refresh Source",
                layer="public_reference",
                source_type="web",
                access_level="public",
                origin="https://example.invalid/refresh",
                allow_online_refresh=True,
                created_by=WORKER_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="manual-v1",
                status=KnowledgeVersionStatus.PUBLISHED.value,
                source_uri="https://example.invalid/refresh",
                created_by=WORKER_ACTOR,
            )
            session.add(version)
            await session.flush()
            session.add(
                KnowledgeWebSnapshot(
                    version_id=version.id,
                    requested_url="https://example.invalid/refresh",
                    final_url="https://example.invalid/refresh",
                    http_status=200,
                    fetched_at=fetched_at,
                )
            )
            await session.flush()
            return source.id


async def _delete_refresh_source(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.source_id == source_id
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.id == source_id
                )
            )


async def _delete_actor_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.created_by == WORKER_ACTOR
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.created_by == WORKER_ACTOR
                )
            )
