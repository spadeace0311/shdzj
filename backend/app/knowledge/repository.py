from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)


class KnowledgeRepository:
    async def get_source(
        self,
        session: AsyncSession,
        source_id: UUID,
        *,
        for_update: bool = False,
    ) -> KnowledgeSource | None:
        statement = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_source_by_key(
        self,
        session: AsyncSession,
        source_key: str,
    ) -> KnowledgeSource | None:
        return await session.scalar(
            select(KnowledgeSource).where(KnowledgeSource.source_key == source_key)
        )

    async def list_sources(self, session: AsyncSession) -> list[KnowledgeSource]:
        statement = select(KnowledgeSource).order_by(
            KnowledgeSource.created_at,
            KnowledgeSource.id,
        )
        return list((await session.scalars(statement)).all())

    async def get_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> KnowledgeSourceVersion | None:
        statement = select(KnowledgeSourceVersion).where(
            KnowledgeSourceVersion.id == version_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_version_by_source_and_label(
        self,
        session: AsyncSession,
        source_id: UUID,
        version: str,
    ) -> KnowledgeSourceVersion | None:
        return await session.scalar(
            select(KnowledgeSourceVersion).where(
                KnowledgeSourceVersion.source_id == source_id,
                KnowledgeSourceVersion.version == version,
            )
        )

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        source_id: UUID | None = None,
    ) -> list[KnowledgeSourceVersion]:
        statement = select(KnowledgeSourceVersion)
        if source_id is not None:
            statement = statement.where(
                KnowledgeSourceVersion.source_id == source_id
            )
        statement = statement.order_by(
            KnowledgeSourceVersion.created_at,
            KnowledgeSourceVersion.id,
        )
        return list((await session.scalars(statement)).all())

    async def list_published_versions(
        self,
        session: AsyncSession,
        source_id: UUID,
        *,
        exclude_id: UUID | None = None,
        for_update: bool = False,
    ) -> list[KnowledgeSourceVersion]:
        statement = select(KnowledgeSourceVersion).where(
            KnowledgeSourceVersion.source_id == source_id,
            KnowledgeSourceVersion.status == "published",
        )
        if exclude_id is not None:
            statement = statement.where(KnowledgeSourceVersion.id != exclude_id)
        if for_update:
            statement = statement.with_for_update()
        return list((await session.scalars(statement)).all())

    async def get_index_version(
        self,
        session: AsyncSession,
        source_version_id: UUID,
        *,
        for_update: bool = False,
    ) -> KnowledgeIndexVersion | None:
        statement = select(KnowledgeIndexVersion).where(
            KnowledgeIndexVersion.source_version_id == source_version_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_job(
        self,
        session: AsyncSession,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> KnowledgeJob | None:
        statement = select(KnowledgeJob).where(KnowledgeJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def list_jobs(self, session: AsyncSession) -> list[KnowledgeJob]:
        statement = select(KnowledgeJob).order_by(
            KnowledgeJob.created_at.desc(),
            KnowledgeJob.id.desc(),
        )
        return list((await session.scalars(statement)).all())

    async def list_pending_jobs(
        self,
        session: AsyncSession,
        *,
        version_id: UUID,
        job_type: str,
    ) -> list[KnowledgeJob]:
        statement = (
            select(KnowledgeJob)
            .where(
                KnowledgeJob.version_id == version_id,
                KnowledgeJob.job_type == job_type,
                KnowledgeJob.status.in_(
                    (
                        "queued",
                        "running",
                    )
                ),
            )
            .order_by(KnowledgeJob.created_at, KnowledgeJob.id)
        )
        return list((await session.scalars(statement)).all())
