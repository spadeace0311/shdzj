from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.knowledge.adapters import EmbeddingAdapter
from app.knowledge.chunker import chunk_document
from app.knowledge.domain import KnowledgeJobStatus, KnowledgeVersionStatus
from app.knowledge.fetch import (
    FetchPolicy,
    build_pinned_http_client,
    fetch_web_document,
    load_fetch_policy,
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
        self._now = now or (lambda: datetime.now(UTC))

    async def process_one(self, session: AsyncSession) -> bool:
        job = await _claim_next_job(
            session,
            lease_seconds=self._lease_seconds,
            now=self._now(),
        )
        if job is None:
            return False

        force_rebuild = bool(job.request_payload.get("force"))
        try:
            async with session.begin_nested():
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
            await _mark_failure(session, job.id, exc, self._now())
        return True

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
        stored = self._store.store_upload(
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

        existing_chunks = await _chunks_for_version(session, version.id)
        existing_index = await session.scalar(
            select(KnowledgeIndexVersion)
            .where(KnowledgeIndexVersion.source_version_id == version.id)
            .with_for_update()
        )
        if (
            not force_rebuild
            and existing_chunks
            and existing_index is not None
            and version.status
            in {
                KnowledgeVersionStatus.INDEXED.value,
                KnowledgeVersionStatus.PUBLISHED.value,
            }
        ):
            return

        if existing_chunks:
            chunks = existing_chunks
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
            chunks = await _persist_chunks(session, version, drafts)

        await _build_index(
            session,
            self._index,
            self._embeddings,
            version,
            source,
            chunks,
            force_rebuild=force_rebuild,
        )
        version.indexed_at = self._now()
        if force_rebuild:
            job.result_payload = {
                "status": "rebuilt",
                "version_id": str(version.id),
                "chunk_count": len(chunks),
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


async def run_worker() -> None:
    from app.db import SessionFactory

    worker = KnowledgeWorker()
    while True:
        processed = False
        async with SessionFactory() as session:
            async with session.begin():
                processed = await worker.process_one(session)
        if not processed:
            await asyncio.sleep(settings.knowledge_worker_poll_seconds)


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
    safe_error = _safe_error_text(error)
    job.last_error = safe_error
    version = await session.get(
        KnowledgeSourceVersion,
        job.version_id,
        with_for_update=True,
    )
    if job.attempt_count >= job.max_attempts:
        job.status = KnowledgeJobStatus.DEAD_LETTER.value
        job.available_at = now
        job.lease_expires_at = None
        job.completed_at = now
        if version is not None:
            version.status = KnowledgeVersionStatus.FAILED.value
            version.failure_reason = safe_error
    else:
        job.status = KnowledgeJobStatus.QUEUED.value
        job.available_at = now + timedelta(
            seconds=min(2 ** max(job.attempt_count - 1, 0), 300)
        )
        job.lease_expires_at = None
        job.completed_at = None
        if version is not None and version.status not in {
            KnowledgeVersionStatus.INDEXED.value,
            KnowledgeVersionStatus.PUBLISHED.value,
        }:
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


async def _chunks_for_version(
    session: AsyncSession,
    version_id: UUID,
) -> list[KnowledgeChunk]:
    return list(
        (
            await session.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.version_id == version_id)
                .order_by(KnowledgeChunk.chunk_no)
                .with_for_update()
            )
        ).all()
    )


async def _persist_chunks(
    session: AsyncSession,
    version: KnowledgeSourceVersion,
    drafts: list[Any],
) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
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
        chunks.append(chunk)
    await session.flush()
    for chunk in chunks:
        chunk.qdrant_point_id = uuid5(NAMESPACE_URL, str(chunk.id))
    await session.flush()
    return chunks


async def _build_index(
    session: AsyncSession,
    index: Any,
    embeddings: Any,
    version: KnowledgeSourceVersion,
    source: KnowledgeSource,
    chunks: list[KnowledgeChunk],
    *,
    force_rebuild: bool,
) -> None:
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
            chunk_count=len(chunks),
            manifest=manifest,
        )
        session.add(index_version)
    else:
        index_version.collection_name = collection_name
        index_version.chunk_count = len(chunks)
        index_version.manifest = manifest
        if not index_was_published:
            index_version.status = KnowledgeVersionStatus.INDEXED.value
    await session.flush()

    version.status = KnowledgeVersionStatus.EMBEDDING.value
    await index.ensure_collection(index_version)
    if force_rebuild:
        await index.delete_version(index_version, version.id)
    batch = await embeddings.embed([chunk.text for chunk in chunks])
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
        for chunk in chunks
    ]
    await index.upsert_chunks(index_version, indexed_chunks, batch)
    if version_was_published:
        version.status = KnowledgeVersionStatus.PUBLISHED.value
    else:
        version.status = KnowledgeVersionStatus.INDEXED.value


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
    asyncio.run(run_worker())
