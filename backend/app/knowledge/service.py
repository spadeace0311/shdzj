from __future__ import annotations

from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.knowledge.domain import KnowledgeJobStatus, KnowledgeVersionStatus
from app.knowledge.models import (
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.publication import KnowledgePublicationService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
    KnowledgeJobResponse,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    KnowledgeVersionCreate,
    KnowledgeVersionResponse,
)
from app.knowledge.storage import KnowledgeFileStore


class KnowledgeService:
    def __init__(
        self,
        *,
        store: KnowledgeFileStore | None = None,
        repository: KnowledgeRepository | None = None,
        publication: KnowledgePublicationService | None = None,
    ) -> None:
        self._store = store or KnowledgeFileStore(
            settings.knowledge_storage_root,
            max_upload_bytes=settings.knowledge_max_upload_bytes,
        )
        self._repository = repository or KnowledgeRepository()
        self._publication = publication or KnowledgePublicationService(
            self._repository
        )

    async def create_source(
        self,
        session: AsyncSession,
        actor: str,
        request: KnowledgeSourceCreate,
    ) -> KnowledgeSource:
        existing = await self._repository.get_source_by_key(
            session,
            request.source_key,
        )
        if existing is not None:
            raise ValueError("knowledge source already exists")
        source = KnowledgeSource(
            source_key=request.source_key,
            title=request.title,
            layer=request.layer.value,
            source_type=request.source_type,
            access_level=request.access_level,
            origin=request.origin,
            allow_online_refresh=request.allow_online_refresh,
            created_by=actor,
        )
        session.add(source)
        await session.flush()
        return source

    async def create_file_version(
        self,
        session: AsyncSession,
        actor: str,
        source_id: UUID,
        request: KnowledgeVersionCreate,
        upload: BinaryIO | UploadFile,
    ) -> KnowledgeVersionResponse:
        source = await self._repository.get_source(
            session,
            source_id,
            for_update=True,
        )
        if source is None:
            raise LookupError("knowledge source not found")
        await self._ensure_unique_version(session, source_id, request.version)

        file_name = getattr(upload, "filename", None) or "upload"
        mime_type = getattr(upload, "content_type", None) or (
            "application/octet-stream"
        )
        source_stream = upload.file if hasattr(upload, "filename") else upload
        stored = self._store.store_upload(
            source_stream,
            file_name=file_name,
            source_id=str(source_id),
            version=request.version,
        )
        version = KnowledgeSourceVersion(
            source_id=source.id,
            version=request.version,
            status=KnowledgeVersionStatus.UPLOADED.value,
            file_name=stored.file_name,
            mime_type=mime_type,
            source_uri=request.source_uri,
            storage_path=stored.managed_path,
            checksum=stored.checksum,
            size_bytes=stored.size_bytes,
            version_metadata=request.metadata,
            created_by=actor,
        )
        session.add(version)
        await session.flush()
        await self._queue_job(
            session,
            version.id,
            job_type="ingest",
            payload={
                "file_name": stored.file_name,
                "source_uri": request.source_uri,
                "metadata": request.metadata,
            },
        )
        return _version_response(version)

    async def create_url_version(
        self,
        session: AsyncSession,
        actor: str,
        source_id: UUID,
        request: KnowledgeVersionCreate,
    ) -> KnowledgeVersionResponse:
        source = await self._repository.get_source(
            session,
            source_id,
            for_update=True,
        )
        if source is None:
            raise LookupError("knowledge source not found")
        if not request.source_uri:
            raise ValueError("source_uri is required for URL versions")
        await self._ensure_unique_version(session, source_id, request.version)
        version = KnowledgeSourceVersion(
            source_id=source.id,
            version=request.version,
            status=KnowledgeVersionStatus.REGISTERED.value,
            source_uri=request.source_uri,
            version_metadata=request.metadata,
            created_by=actor,
        )
        session.add(version)
        await session.flush()
        await self._queue_job(
            session,
            version.id,
            job_type="fetch",
            payload={
                "source_uri": request.source_uri,
                "metadata": request.metadata,
            },
        )
        return _version_response(version)

    async def list_sources(
        self,
        session: AsyncSession,
    ) -> list[KnowledgeSourceResponse]:
        sources = await self._repository.list_sources(session)
        return [_source_response(source) for source in sources]

    async def get_version(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> KnowledgeVersionResponse:
        version = await self._repository.get_version(session, version_id)
        if version is None:
            raise LookupError("knowledge source version not found")
        return _version_response(version)

    async def list_jobs(
        self,
        session: AsyncSession,
    ) -> list[KnowledgeJobResponse]:
        jobs = await self._repository.list_jobs(session)
        return [_job_response(job) for job in jobs]

    async def publish_version(
        self,
        session: AsyncSession,
        actor: str,
        version_id: UUID,
        reason: str,
    ) -> KnowledgeVersionResponse:
        version = await self._publication.publish(
            session,
            version_id,
            actor,
            reason,
        )
        return _version_response(version)

    async def rollback_version(
        self,
        session: AsyncSession,
        actor: str,
        version_id: UUID,
        reason: str,
    ) -> KnowledgeVersionResponse:
        version = await self._publication.rollback(
            session,
            version_id,
            actor,
            reason,
        )
        return _version_response(version)

    async def retry_job(
        self,
        session: AsyncSession,
        actor: str,
        job_id: UUID,
    ) -> KnowledgeJobResponse:
        del actor
        job = await self._repository.get_job(
            session,
            job_id,
            for_update=True,
        )
        if job is None:
            raise LookupError("knowledge job not found")
        if job.status not in {
            KnowledgeJobStatus.FAILED.value,
            KnowledgeJobStatus.DEAD_LETTER.value,
        }:
            raise ValueError("only failed knowledge jobs can be retried")
        job.status = KnowledgeJobStatus.QUEUED.value
        job.attempt_count = 0
        job.available_at = datetime.now(UTC)
        job.lease_expires_at = None
        job.last_error = None
        job.completed_at = None
        await session.flush()
        return _job_response(job)

    async def _ensure_unique_version(
        self,
        session: AsyncSession,
        source_id: UUID,
        version: str,
    ) -> None:
        existing = await self._repository.get_version_by_source_and_label(
            session,
            source_id,
            version,
        )
        if existing is not None:
            raise ValueError("knowledge source version already exists")

    async def _queue_job(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        job_type: str,
        payload: dict,
    ) -> KnowledgeJob:
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


def _source_response(source: KnowledgeSource) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse(
        id=source.id,
        source_key=source.source_key,
        title=source.title,
        layer=source.layer,
        source_type=source.source_type,
        access_level=source.access_level,
        origin=source.origin,
        allow_online_refresh=source.allow_online_refresh,
        is_active=source.is_active,
        created_by=source.created_by,
        created_at=source.created_at or datetime.now(UTC),
    )


def _version_response(
    version: KnowledgeSourceVersion,
) -> KnowledgeVersionResponse:
    return KnowledgeVersionResponse(
        id=version.id,
        source_id=version.source_id,
        version=version.version,
        status=version.status,
        checksum=version.checksum,
        size_bytes=version.size_bytes,
        failure_reason=version.failure_reason,
        created_at=version.created_at or datetime.now(UTC),
        published_at=version.published_at,
    )


def _job_response(job: KnowledgeJob) -> KnowledgeJobResponse:
    return KnowledgeJobResponse(
        id=job.id,
        version_id=job.version_id,
        job_type=job.job_type,
        status=job.status,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        available_at=job.available_at or datetime.now(UTC),
        lease_expires_at=job.lease_expires_at,
        request_payload=dict(job.request_payload or {}),
        result_payload=dict(job.result_payload or {}),
        last_error=job.last_error,
        created_at=job.created_at or datetime.now(UTC),
        completed_at=job.completed_at,
    )
