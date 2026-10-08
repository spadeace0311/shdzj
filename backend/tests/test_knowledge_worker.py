from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.embedding.schemas import DENSE_DIMENSIONS, EmbeddingBatch
from app.knowledge.domain import KnowledgeJobStatus, KnowledgeVersionStatus
from app.knowledge.index import IndexedChunk
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.publication import KnowledgePublicationService
from app.knowledge.storage import KnowledgeFileStore
from app.knowledge.worker import KnowledgeWorker


WORKER_ACTOR = "knowledge-worker-test"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_ingest_job_parses_chunks_and_indexes(session_factory, tmp_path: Path):
    try:
        store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024 * 1024)
        stored = store.store_upload(
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
        stored = store.store_upload(
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


async def test_worker_dead_letters_after_attempt_budget(
    session_factory,
    tmp_path: Path,
):
    try:
        version_id = await _seed_version_and_job(
            session_factory,
            status=KnowledgeVersionStatus.UPLOADED.value,
            job_type="ingest",
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


async def _process_one(session_factory, worker: KnowledgeWorker) -> bool:
    async with session_factory() as session:
        async with session.begin():
            return await worker.process_one(session)


class _FakeIndex:
    def __init__(self) -> None:
        self.upsert_count = 0

    async def ensure_collection(self, index_version: KnowledgeIndexVersion) -> None:
        del index_version

    async def upsert_chunks(
        self,
        index_version: KnowledgeIndexVersion,
        chunks: list[IndexedChunk],
        embeddings: EmbeddingBatch,
    ) -> None:
        del index_version, chunks, embeddings
        self.upsert_count += 1


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.embed_count = 0

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.embed_count += 1
        return EmbeddingBatch(
            dense=[[0.1] * DENSE_DIMENSIONS for _ in texts],
            sparse=[{} for _ in texts],
        )


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
) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = KnowledgeJob(
                version_id=version_id,
                job_type=job_type,
                status=KnowledgeJobStatus.QUEUED.value,
                max_attempts=settings.knowledge_job_max_attempts,
                request_payload=payload or {},
            )
            session.add(job)
            await session.flush()


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
