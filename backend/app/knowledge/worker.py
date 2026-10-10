from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.knowledge.adapters import EmbeddingAdapter
from app.knowledge.chunker import chunk_document
from app.knowledge.domain import (
    KnowledgeJobStatus,
    KnowledgeVersionDisabledError,
    KnowledgeVersionStatus,
)
from app.knowledge.fetch import (
    FetchPolicy,
    build_pinned_http_client,
    fetch_web_document,
    load_fetch_policy,
)
from app.knowledge.health import (
    WorkerHealthState,
    run_health_server,
    set_worker_health_state,
)
from app.knowledge.index import IndexedChunk, KnowledgeIndex
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
    KnowledgeWebSnapshot,
)
from app.knowledge.parser import DocumentParser, ParsedDocument
from app.knowledge.publication import KnowledgePublicationService
from app.knowledge.storage import KnowledgeFileStore


_FETCH_TIMEOUT_SECONDS = 30.0
_SUPPORTED_JOB_TYPES = {"fetch", "ingest", "index", "publish", "rollback"}
_REPEATABLE_JOB_TYPES = {"fetch", "ingest", "index"}


class KnowledgeWorker:
    def __init__(
        self,
        *,
        store: KnowledgeFileStore | None = None,
        parser: DocumentParser | None = None,
        chunker: Callable[..., list[Any]] | None = None,
        index: Any | None = None,
        embeddings: Any | None = None,
        publication: KnowledgePublicationService | None = None,
        fetch_policy: FetchPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
        lease_seconds: int | None = None,
        batch_size: int | None = None,
        refresh_interval_seconds: int | None = None,
        online_search_enabled: bool | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store or KnowledgeFileStore(
            settings.knowledge_storage_root,
            max_upload_bytes=settings.knowledge_max_upload_bytes,
        )
        self._parser = parser or DocumentParser()
        self._chunker = chunker or chunk_document
        self._index = index or KnowledgeIndex()
        self._embeddings = embeddings or EmbeddingAdapter()
        self._publication = publication or KnowledgePublicationService()
        self._fetch_policy = fetch_policy or load_fetch_policy()
        self._http_client = http_client
        self._lease_seconds = lease_seconds or settings.knowledge_job_lease_seconds
        self._batch_size = batch_size or settings.knowledge_worker_batch_size
        self._refresh_interval_seconds = (
            refresh_interval_seconds
            if refresh_interval_seconds is not None
            else settings.knowledge_online_refresh_interval_seconds
        )
        self._online_search_enabled = (
            settings.online_search_enabled
            if online_search_enabled is None
            else online_search_enabled
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._pending_alias_rebuild: Any | None = None

    async def process_one(self, session: AsyncSession) -> bool:
        self._pending_alias_rebuild = None
        job = await _claim_next_job(
            session,
            lease_seconds=self._lease_seconds,
            now=self._now(),
        )
        if job is None:
            return False

        job_id = job.id
        force_rebuild = bool(job.request_payload.get("force"))
        try:
            async with session.begin_nested():
                version = await _version_for_update(session, job.version_id)
                if version.status == KnowledgeVersionStatus.DISABLED.value and not (
                    job.job_type == "index" and force_rebuild
                ):
                    raise KnowledgeVersionDisabledError(
                        "disabled knowledge versions cannot run live jobs"
                    )
                if (
                    job.job_type in _REPEATABLE_JOB_TYPES
                    and not force_rebuild
                    and await self._existing_success(
                        session,
                        job.version_id,
                        job.job_type,
                    )
                ):
                    await _mark_success(session, job.id, self._now())
                else:
                    await self._dispatch(
                        session,
                        job,
                        force_rebuild=force_rebuild,
                    )
                    await _mark_success(session, job.id, self._now())
        except Exception as exc:
            if self._pending_alias_rebuild is not None:
                await self.compensate_pending_alias_rebuild()
            await _mark_failure(session, job_id, exc, self._now())
        return True

    def clear_pending_alias_rebuild(self) -> None:
        self._pending_alias_rebuild = None

    async def compensate_pending_alias_rebuild(self) -> None:
        rebuild = self._pending_alias_rebuild
        self._pending_alias_rebuild = None
        if rebuild is None:
            return
        await self._index.compensate_rebuild(rebuild)
        await self._index.abort_rebuild(rebuild)

    async def _existing_success(
        self,
        session: AsyncSession,
        version_id: UUID,
        job_type: str,
    ) -> KnowledgeJob | None:
        return await session.scalar(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.version_id == version_id,
                KnowledgeJob.job_type == job_type,
                KnowledgeJob.status == KnowledgeJobStatus.SUCCEEDED.value,
            )
            .order_by(KnowledgeJob.completed_at.desc())
            .limit(1)
        )

    async def _dispatch(
        self,
        session: AsyncSession,
        job: KnowledgeJob,
        *,
        force_rebuild: bool,
    ) -> None:
        if job.job_type not in _SUPPORTED_JOB_TYPES:
            raise ValueError(f"unsupported knowledge job type: {job.job_type}")
        if job.job_type == "fetch":
            await self._handle_fetch(session, job)
            return
        if job.job_type in {"ingest", "index"}:
            await self._handle_ingest(
                session,
                job,
                force_rebuild=force_rebuild,
            )
            return
        if job.job_type == "publish":
            await self._handle_publish(session, job)
            return
        await self._handle_rollback(session, job)

    async def _handle_fetch(self, session: AsyncSession, job: KnowledgeJob) -> None:
        version = await _version_for_update(session, job.version_id)
        source_uri = str(job.request_payload.get("source_uri") or "")
        if not source_uri:
            raise ValueError("fetch job is missing source_uri")

        if self._http_client is not None:
            fetched = await fetch_web_document(
                source_uri,
                self._fetch_policy,
                self._http_client,
                online_search_enabled=settings.online_search_enabled,
            )
        else:
            async with build_pinned_http_client(
                timeout=_FETCH_TIMEOUT_SECONDS,
            ) as client:
                fetched = await fetch_web_document(
                    source_uri,
                    self._fetch_policy,
                    client,
                    online_search_enabled=settings.online_search_enabled,
                )

        file_name = _file_name_from_url(
            fetched.final_url,
            fetched.content_type,
        )
        stored = await self._store.store_upload(
            BytesIO(fetched.body),
            file_name=file_name,
            source_id=str(version.source_id),
            version=version.version,
        )
        version.status = KnowledgeVersionStatus.UPLOADED.value
        version.file_name = stored.file_name
        version.mime_type = fetched.content_type
        version.source_uri = fetched.final_url
        version.storage_path = stored.managed_path
        version.checksum = fetched.checksum
        version.size_bytes = len(fetched.body)
        session.add(
            KnowledgeWebSnapshot(
                version_id=version.id,
                requested_url=fetched.requested_url,
                final_url=fetched.final_url,
                http_status=fetched.http_status,
                content_type=fetched.content_type,
                headers=fetched.headers,
                body_text=(
                    fetched.text
                    if (fetched.content_type or "").startswith("text/")
                    else None
                ),
                checksum=fetched.checksum,
                fetched_at=fetched.fetched_at,
            )
        )
        await _queue_job(
            session,
            version.id,
            "ingest",
            {
                "file_name": stored.file_name,
                "source_uri": fetched.final_url,
                "metadata": dict(job.request_payload.get("metadata") or {}),
            },
        )

    async def _handle_ingest(
        self,
        session: AsyncSession,
        job: KnowledgeJob,
        *,
        force_rebuild: bool,
    ) -> None:
        version = await _version_for_update(session, job.version_id)
        source = await session.get(KnowledgeSource, version.source_id)
        if source is None:
            raise LookupError("knowledge source not found")

        existing_chunk_count = await _chunk_count_for_version(
            session,
            version.id,
        )
        existing_index = await session.scalar(
            select(KnowledgeIndexVersion)
            .where(KnowledgeIndexVersion.source_version_id == version.id)
            .with_for_update()
        )
        if (
            not force_rebuild
            and existing_chunk_count > 0
            and existing_index is not None
            and version.status
            in {
                KnowledgeVersionStatus.INDEXED.value,
                KnowledgeVersionStatus.PUBLISHED.value,
            }
        ):
            return

        if existing_chunk_count > 0:
            chunk_count = existing_chunk_count
        else:
            if not version.storage_path:
                raise ValueError("version has no stored document to ingest")
            version.status = KnowledgeVersionStatus.PARSING.value
            parsed = self._parser.parse(
                version.storage_path,
                file_name=version.file_name or "document",
                mime_type=version.mime_type,
            )
            await _persist_parse_result(
                self._store,
                version,
                parsed,
            )
            drafts = list(self._chunker(parsed))
            if not drafts:
                raise ValueError("document produced no indexable chunks")
            chunk_count = await _persist_chunks(
                session,
                version,
                drafts,
                batch_size=self._batch_size,
            )

        activated_rebuild = await _build_index(
            session,
            self._index,
            self._embeddings,
            version,
            source,
            chunk_count,
            force_rebuild=force_rebuild,
            batch_size=self._batch_size,
        )
        self._pending_alias_rebuild = activated_rebuild
        version.indexed_at = self._now()
        if force_rebuild:
            job.result_payload = {
                "status": "rebuilt",
                "version_id": str(version.id),
                "chunk_count": chunk_count,
            }

    async def _handle_publish(self, session: AsyncSession, job: KnowledgeJob) -> None:
        actor = str(job.request_payload.get("actor") or "knowledge-worker")
        reason = str(job.request_payload.get("reason") or "worker publish")
        version = await _version_for_update(session, job.version_id)
        if version.status == KnowledgeVersionStatus.PUBLISHED.value:
            job.result_payload = {
                "status": "already_published",
                "version_id": str(version.id),
            }
            return
        version = await self._publication.publish(
            session,
            job.version_id,
            actor,
            reason,
        )
        job.result_payload = {
            "status": "published",
            "version_id": str(version.id),
        }

    async def _handle_rollback(self, session: AsyncSession, job: KnowledgeJob) -> None:
        actor = str(job.request_payload.get("actor") or "knowledge-worker")
        reason = str(job.request_payload.get("reason") or "worker rollback")
        version = await _version_for_update(session, job.version_id)
        if version.status == KnowledgeVersionStatus.PUBLISHED.value:
            job.result_payload = {
                "status": "already_published",
                "version_id": str(version.id),
            }
            return
        version = await self._publication.rollback(
            session,
            job.version_id,
            actor,
            reason,
        )
        job.result_payload = {
            "status": "published",
            "version_id": str(version.id),
        }

    async def enqueue_due_refreshes(self, session: AsyncSession) -> int:
        if not self._online_search_enabled:
            return 0
        now = self._now()
        interval = self._refresh_interval_seconds
        bucket_timestamp = int(now.timestamp() // interval * interval)
        bucket_label = f"auto-refresh-{bucket_timestamp}"
        sources = list(
            (
                await session.scalars(
                    select(KnowledgeSource)
                    .where(
                        KnowledgeSource.allow_online_refresh.is_(True),
                        KnowledgeSource.is_active.is_(True),
                    )
                    .order_by(KnowledgeSource.id)
                    .with_for_update()
                )
            ).all()
        )
        enqueued = 0
        for source in sources:
            latest_version = await session.scalar(
                select(KnowledgeSourceVersion)
                .where(KnowledgeSourceVersion.source_id == source.id)
                .order_by(
                    KnowledgeSourceVersion.created_at.desc(),
                    KnowledgeSourceVersion.id.desc(),
                )
                .limit(1)
            )
            latest_fetch = await session.scalar(
                select(KnowledgeWebSnapshot.fetched_at)
                .join(
                    KnowledgeSourceVersion,
                    KnowledgeWebSnapshot.version_id
                    == KnowledgeSourceVersion.id,
                )
                .where(KnowledgeSourceVersion.source_id == source.id)
                .order_by(KnowledgeWebSnapshot.fetched_at.desc())
                .limit(1)
            )
            source_uri = source.origin or (
                latest_version.source_uri if latest_version is not None else None
            )
            if not source_uri or latest_fetch is None:
                continue
            if latest_fetch + timedelta(seconds=interval) > now:
                continue
            existing = await session.scalar(
                select(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.source_id == source.id,
                    KnowledgeSourceVersion.version == bucket_label,
                )
            )
            if existing is not None:
                continue
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version=bucket_label,
                status=KnowledgeVersionStatus.REGISTERED.value,
                source_uri=source_uri,
                version_metadata={
                    **(latest_version.version_metadata or {}),
                    "auto_refresh": True,
                    "refresh_bucket": bucket_timestamp,
                },
                created_by="knowledge-refresh",
            )
            session.add(version)
            await session.flush()
            await _queue_job(
                session,
                version.id,
                "fetch",
                {
                    "source_uri": source_uri,
                    "metadata": dict(version.version_metadata or {}),
                    "refresh": True,
                },
            )
            enqueued += 1
        return enqueued


async def run_worker() -> None:
    from app.db import SessionFactory

    worker = KnowledgeWorker()
    state = WorkerHealthState(
        stale_seconds=max(
            30.0,
            settings.knowledge_worker_poll_seconds * 10,
        )
    )
    set_worker_health_state(state)
    health_task = asyncio.create_task(
        run_health_server(settings.knowledge_worker_health_port)
    )
    try:
        while True:
            state.mark_alive()
            processed = False
            async with SessionFactory() as session:
                try:
                    async with session.begin():
                        processed = (
                            await worker.enqueue_due_refreshes(session) > 0
                        ) or processed
                        processed = await worker.process_one(session) or processed
                except BaseException:
                    await worker.compensate_pending_alias_rebuild()
                    raise
                else:
                    worker.clear_pending_alias_rebuild()
            if not processed:
                await asyncio.sleep(settings.knowledge_worker_poll_seconds)
    finally:
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)


async def run_refresh_scan_once() -> int:
    from app.db import SessionFactory

    async with SessionFactory() as session:
        async with session.begin():
            return await KnowledgeWorker().enqueue_due_refreshes(session)


async def _claim_next_job(
    session: AsyncSession,
    *,
    lease_seconds: int,
    now: datetime,
) -> KnowledgeJob | None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('knowledge-worker'))")
    )
    lease_until = now + timedelta(seconds=lease_seconds)
    job = await session.scalar(
        select(KnowledgeJob)
        .where(
            KnowledgeJob.available_at <= now,
            or_(
                KnowledgeJob.status == KnowledgeJobStatus.QUEUED.value,
                and_(
                    KnowledgeJob.status == KnowledgeJobStatus.RUNNING.value,
                    KnowledgeJob.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(KnowledgeJob.available_at, KnowledgeJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = KnowledgeJobStatus.RUNNING.value
    job.attempt_count += 1
    job.available_at = lease_until
    job.lease_expires_at = lease_until
    job.last_error = None
    job.completed_at = None
    await session.flush()
    return job


async def _mark_success(
    session: AsyncSession,
    job_id: UUID,
    now: datetime,
) -> None:
    job = await session.get(KnowledgeJob, job_id, with_for_update=True)
    if job is None:
        return
    job.status = KnowledgeJobStatus.SUCCEEDED.value
    job.available_at = now
    job.lease_expires_at = None
    job.last_error = None
    job.completed_at = now
    await session.flush()


async def _mark_failure(
    session: AsyncSession,
    job_id: UUID,
    error: Exception,
    now: datetime,
) -> None:
    job = await session.get(KnowledgeJob, job_id, with_for_update=True)
    if job is None:
        return
    await session.refresh(job, with_for_update=True)
    safe_error = _safe_error_text(error)
    job.last_error = safe_error
    version = await session.get(
        KnowledgeSourceVersion,
        job.version_id,
        with_for_update=True,
    )
    if version is not None:
        await session.refresh(version, with_for_update=True)
    if job.attempt_count >= job.max_attempts:
        job.status = KnowledgeJobStatus.DEAD_LETTER.value
        job.available_at = now
        job.lease_expires_at = None
        job.completed_at = now
        preserve_healthy_rebuild_target = (
            job.job_type == "index"
            and job.request_payload.get("force") is True
            and version is not None
            and version.status
            in {
                KnowledgeVersionStatus.INDEXED.value,
                KnowledgeVersionStatus.PUBLISHED.value,
                KnowledgeVersionStatus.DISABLED.value,
            }
        )
        if (
            version is not None
            and not preserve_healthy_rebuild_target
            and version.status != KnowledgeVersionStatus.DISABLED.value
        ):
            version.status = KnowledgeVersionStatus.FAILED.value
            version.failure_reason = safe_error
    else:
        job.status = KnowledgeJobStatus.QUEUED.value
        job.available_at = now + timedelta(
            seconds=min(2 ** max(job.attempt_count - 1, 0), 300)
        )
        job.lease_expires_at = None
        job.completed_at = None
        if (
            version is not None
            and version.status != KnowledgeVersionStatus.DISABLED.value
            and version.status
            not in {
                KnowledgeVersionStatus.INDEXED.value,
                KnowledgeVersionStatus.PUBLISHED.value,
            }
        ):
            version.status = KnowledgeVersionStatus.FAILED.value
            version.failure_reason = safe_error
    await session.flush()


async def _version_for_update(
    session: AsyncSession,
    version_id: UUID,
) -> KnowledgeSourceVersion:
    version = await session.get(
        KnowledgeSourceVersion,
        version_id,
        with_for_update=True,
    )
    if version is None:
        raise LookupError("knowledge source version not found")
    return version


async def _persist_parse_result(
    store: KnowledgeFileStore,
    version: KnowledgeSourceVersion,
    parsed: ParsedDocument,
) -> None:
    text_value = "\n\n".join(block.text for block in parsed.blocks)
    stored = store.write_text("parsed.txt", text_value)
    version.parsed_text_path = stored.relative_path
    version.status = KnowledgeVersionStatus.PARSED.value
    version.parse_manifest = {
        "title": parsed.title,
        "block_count": len(parsed.blocks),
    }


async def _chunk_count_for_version(
    session: AsyncSession,
    version_id: UUID,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.version_id == version_id)
        )
        or 0
    )


async def _persist_chunks(
    session: AsyncSession,
    version: KnowledgeSourceVersion,
    drafts: list[Any],
    *,
    batch_size: int,
) -> int:
    pending: list[KnowledgeChunk] = []
    chunk_count = 0

    async def flush_batch() -> None:
        nonlocal pending
        if not pending:
            return
        await session.flush()
        for chunk in pending:
            chunk.qdrant_point_id = uuid5(NAMESPACE_URL, str(chunk.id))
        await session.flush()
        for chunk in pending:
            session.expunge(chunk)
        pending = []

    for chunk_no, draft in enumerate(drafts, start=1):
        metadata = dict(getattr(draft, "metadata", {}) or {})
        chunk = KnowledgeChunk(
            version_id=version.id,
            chunk_no=chunk_no,
            section_path=list(draft.section_path),
            page_from=draft.page_from,
            page_to=draft.page_to,
            table_range=(
                list(draft.table_range) if draft.table_range is not None else None
            ),
            checksum=str(metadata.get("checksum") or ""),
            chunk_metadata={
                key: value for key, value in metadata.items() if key != "checksum"
            },
            search_text=draft.text,
            text=draft.text,
        )
        session.add(chunk)
        pending.append(chunk)
        chunk_count += 1
        if len(pending) >= batch_size:
            await flush_batch()
    await flush_batch()
    return chunk_count


async def _chunk_batches(
    session: AsyncSession,
    version_id: UUID,
    *,
    batch_size: int,
):
    result = await session.stream_scalars(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.version_id == version_id)
        .order_by(KnowledgeChunk.chunk_no, KnowledgeChunk.id)
        .execution_options(yield_per=batch_size)
    )
    async for batch in result.partitions(batch_size):
        yield batch
        for chunk in batch:
            session.expunge(chunk)


async def _build_index(
    session: AsyncSession,
    index: Any,
    embeddings: Any,
    version: KnowledgeSourceVersion,
    source: KnowledgeSource,
    chunk_count: int,
    *,
    force_rebuild: bool,
    batch_size: int,
) -> Any | None:
    index_version = await session.scalar(
        select(KnowledgeIndexVersion)
        .where(KnowledgeIndexVersion.source_version_id == version.id)
        .with_for_update()
    )
    version_was_published = (
        version.status == KnowledgeVersionStatus.PUBLISHED.value
    )
    index_was_published = (
        index_version is not None
        and index_version.status == KnowledgeVersionStatus.PUBLISHED.value
    )
    manifest = {
        "source_title": source.title,
    }
    if version.source_uri is not None:
        manifest["source_uri"] = version.source_uri
    event_id = _event_id_from_metadata(version.version_metadata)
    if event_id is not None:
        manifest["event_id"] = str(event_id)
    collection_name = f"{settings.qdrant_collection_prefix}-{source.source_key}"
    if index_version is None:
        index_version = KnowledgeIndexVersion(
            source_version_id=version.id,
            version=version.version,
            status=KnowledgeVersionStatus.INDEXED.value,
            collection_name=collection_name,
            embedding_model=settings.embedding_model_name,
            reranker_model=settings.reranker_model_name,
            chunk_count=chunk_count,
            manifest=manifest,
        )
        session.add(index_version)
    else:
        index_version.chunk_count = chunk_count
        index_version.manifest = manifest
        if not index_was_published:
            index_version.status = KnowledgeVersionStatus.INDEXED.value
    await session.flush()

    version.status = KnowledgeVersionStatus.EMBEDDING.value
    rebuild = None
    target_collection = None
    activated_rebuild = None
    try:
        await index.ensure_collection(index_version)
        if force_rebuild:
            rebuild = await index.prepare_rebuild(index_version)
            target_collection = rebuild.staging_name
        async for chunk_batch in _chunk_batches(
            session,
            version.id,
            batch_size=batch_size,
        ):
            batch = await embeddings.embed(
                [chunk.text for chunk in chunk_batch]
            )
            indexed_chunks = [
                IndexedChunk(
                    chunk_id=chunk.id,
                    version_id=version.id,
                    source_id=source.id,
                    source_key=source.source_key,
                    layer=source.layer,
                    access_level=source.access_level,
                    text=chunk.text,
                    section_path=tuple(chunk.section_path or ()),
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    checksum=chunk.checksum,
                )
                for chunk in chunk_batch
            ]
            await index.upsert_chunks(
                index_version,
                indexed_chunks,
                batch,
                collection_name=target_collection,
            )
        manifest["index_batch_size"] = batch_size
        if rebuild is not None:
            activated_rebuild = rebuild
            await index.commit_rebuild(rebuild)
            manifest["collection_alias"] = rebuild.alias_name
            manifest["physical_collection_name"] = rebuild.staging_name
            index_version.collection_name = rebuild.alias_name
        index_version.manifest = manifest
        await session.flush()
    except BaseException:
        if rebuild is not None:
            if activated_rebuild is not None:
                await index.compensate_rebuild(rebuild)
            await index.abort_rebuild(rebuild)
        raise
    if version_was_published:
        version.status = KnowledgeVersionStatus.PUBLISHED.value
    else:
        version.status = KnowledgeVersionStatus.INDEXED.value
    return activated_rebuild


async def _queue_job(
    session: AsyncSession,
    version_id: UUID,
    job_type: str,
    payload: dict[str, Any],
) -> KnowledgeJob | None:
    existing = await session.scalar(
        select(KnowledgeJob)
        .where(
            KnowledgeJob.version_id == version_id,
            KnowledgeJob.job_type == job_type,
            KnowledgeJob.status.in_(
                (
                    KnowledgeJobStatus.QUEUED.value,
                    KnowledgeJobStatus.RUNNING.value,
                )
            ),
        )
        .limit(1)
    )
    if existing is not None:
        return None
    job = KnowledgeJob(
        version_id=version_id,
        job_type=job_type,
        status=KnowledgeJobStatus.QUEUED.value,
        max_attempts=settings.knowledge_job_max_attempts,
        request_payload=payload,
    )
    session.add(job)
    await session.flush()
    return job


def _file_name_from_url(url: str, content_type: str | None) -> str:
    path_name = Path(urlsplit(url).path).name
    if path_name and Path(path_name).suffix:
        return path_name[:255]
    if (content_type or "").startswith("application/pdf"):
        return "document.pdf"
    if (content_type or "").startswith("text/html"):
        return "document.html"
    return "document.txt"


def _event_id_from_metadata(metadata: dict[str, Any] | None) -> UUID | None:
    value = (metadata or {}).get("event_id")
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _safe_error_text(error: Exception) -> str:
    message = " ".join(str(error).replace("\x00", " ").split())
    if not message:
        return type(error).__name__
    message = re.sub(
        r"(?i)\b(?:api[_-]?key|apikey|password|passwd|pwd|secret|"
        r"access[_-]?token)\b\s*[:=]\s*[^\s,;]+",
        "[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/]+:[^\s@/]+@\S+",
        "[redacted-url]",
        message,
    )
    return f"{type(error).__name__}: {message[:1000]}"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh-once":
        asyncio.run(run_refresh_scan_once())
    else:
        asyncio.run(run_worker())
