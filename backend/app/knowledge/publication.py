from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.domain import (
    KnowledgeVersionDisabledError,
    KnowledgeVersionStatus,
)
from app.knowledge.models import (
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.repository import KnowledgeRepository
from app.qa.models import QaAdminAuditLog


class KnowledgePublicationError(ValueError):
    """Base error for invalid knowledge publication operations."""


class KnowledgeVersionNotIndexedError(KnowledgePublicationError):
    """Raised when a non-indexed version is targeted for publication."""


class KnowledgeIndexPointerMissingError(KnowledgePublicationError):
    """Raised when a publication target has no active index pointer."""


class KnowledgeEmptyIndexError(KnowledgePublicationError):
    """Raised when an index pointer does not contain retrievable chunks."""


class KnowledgePublicationService:
    def __init__(
        self,
        repository: KnowledgeRepository | None = None,
    ) -> None:
        self._repository = repository or KnowledgeRepository()

    async def publish(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> KnowledgeSourceVersion:
        target = await self._repository.get_version(
            session,
            version_id,
            for_update=True,
        )
        if target is None:
            raise LookupError("knowledge source version not found")
        if target.status == KnowledgeVersionStatus.DISABLED.value:
            raise KnowledgeVersionDisabledError(
                "disabled knowledge versions cannot be published"
            )
        if target.status != "indexed":
            raise KnowledgeVersionNotIndexedError(
                "only indexed knowledge versions can be published"
            )
        source = await self._lock_source(session, target.source_id)
        previous = await self._repository.list_published_versions(
            session,
            source.id,
            exclude_id=target.id,
            for_update=True,
        )
        now = datetime.now(UTC)
        for published_version in previous:
            published_version.status = "indexed"
            await self._mark_index_version_indexed(
                session,
                published_version.id,
            )
        await self._activate_index_version(
            session,
            source,
            target,
            now,
            require_chunks=True,
        )
        target.status = "published"
        target.published_at = now
        session.add(
            QaAdminAuditLog(
                resource_type="knowledge_version",
                resource_id=str(target.id),
                action="publish",
                actor=actor,
                details={
                    "reason": reason,
                    "old_version_ids": [
                        str(published_version.id)
                        for published_version in previous
                    ],
                    "old_version_id": (
                        str(previous[0].id) if previous else None
                    ),
                    "new_version_id": str(target.id),
                },
                created_at=now,
            )
        )
        await session.flush()
        return target

    async def rollback(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> KnowledgeSourceVersion:
        target = await self._repository.get_version(
            session,
            version_id,
            for_update=True,
        )
        if target is None:
            raise LookupError("knowledge source version not found")
        if target.status == KnowledgeVersionStatus.DISABLED.value:
            raise KnowledgeVersionDisabledError(
                "disabled knowledge versions cannot be rolled back"
            )
        if target.status != "indexed":
            raise KnowledgeVersionNotIndexedError(
                "rollback target must be an indexed knowledge version"
            )
        source = await self._lock_source(session, target.source_id)
        previous = await self._repository.list_published_versions(
            session,
            source.id,
            exclude_id=target.id,
            for_update=True,
        )
        now = datetime.now(UTC)
        await self._activate_index_version(
            session,
            source,
            target,
            now,
            require_chunks=False,
        )
        for published_version in previous:
            published_version.status = "indexed"
            await self._mark_index_version_indexed(
                session,
                published_version.id,
            )
        target.status = "published"
        target.published_at = now
        session.add(
            QaAdminAuditLog(
                resource_type="knowledge_version",
                resource_id=str(target.id),
                action="rollback",
                actor=actor,
                details={
                    "reason": reason,
                    "old_version_ids": [
                        str(published_version.id)
                        for published_version in previous
                    ],
                    "old_version_id": (
                        str(previous[0].id) if previous else None
                    ),
                    "new_version_id": str(target.id),
                },
                created_at=now,
            )
        )
        await session.flush()
        return target

    async def _lock_source(
        self,
        session: AsyncSession,
        source_id: UUID,
    ) -> KnowledgeSource:
        source = await self._repository.get_source(
            session,
            source_id,
            for_update=True,
        )
        if source is None:
            raise LookupError("knowledge source not found")
        return source

    async def _activate_index_version(
        self,
        session: AsyncSession,
        source: KnowledgeSource,
        target: KnowledgeSourceVersion,
        now: datetime,
        *,
        require_chunks: bool,
    ) -> None:
        existing = await self._repository.get_index_version(
            session,
            target.id,
            for_update=True,
        )
        if existing is None:
            raise KnowledgeIndexPointerMissingError(
                "target has no active index pointer"
            )
        if require_chunks and existing.chunk_count <= 0:
            raise KnowledgeEmptyIndexError(
                "target index pointer has no retrievable chunks"
            )
        existing.status = "published"
        existing.activated_at = now
        existing.manifest = _build_index_manifest(source, target, now)
        await session.flush()

    async def _mark_index_version_indexed(
        self,
        session: AsyncSession,
        source_version_id: UUID,
    ) -> None:
        existing = await self._repository.get_index_version(
            session,
            source_version_id,
            for_update=True,
        )
        if existing is not None:
            existing.status = "indexed"


def _build_index_manifest(
    source: KnowledgeSource,
    target: KnowledgeSourceVersion,
    now: datetime,
) -> dict:
    manifest = {
        "source_title": source.title,
        "published_at": now.isoformat(),
    }
    if target.source_uri is not None:
        manifest["source_uri"] = target.source_uri
    event_id = _event_id_from_metadata(target.version_metadata)
    if event_id is not None:
        manifest["event_id"] = str(event_id)
    return manifest


def _event_id_from_metadata(metadata: dict | None) -> UUID | None:
    value = (metadata or {}).get("event_id")
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
