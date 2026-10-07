from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.publication import KnowledgePublicationService
from app.qa.models import QaAdminAuditLog


PUBLICATION_ACTOR = "knowledge-publication-test"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_publish_promotes_indexed_version_and_audits(
    session_factory,
) -> None:
    try:
        async with session_factory() as session:
            async with session.begin():
                source = await _create_source(session)
                current = await _create_version(
                    session,
                    source.id,
                    "v1",
                    "published",
                    checksum="a" * 64,
                )
                target = await _create_version(
                    session,
                    source.id,
                    "v2",
                    "indexed",
                    checksum="b" * 64,
                )
                session.add(
                    KnowledgeIndexVersion(
                        source_version_id=current.id,
                        version="v1",
                        status="published",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                    )
                )
                await session.flush()

        async with session_factory() as session:
            async with session.begin():
                published = await KnowledgePublicationService().publish(
                    session,
                    target.id,
                    PUBLICATION_ACTOR,
                    "release v2",
                )

        async with session_factory() as session:
            assert published.id == target.id
            assert published.status == "published"
            current = await session.get(KnowledgeSourceVersion, current.id)
            assert current is not None
            assert current.status == "indexed"
            index_version = await session.scalar(
                select(KnowledgeIndexVersion).where(
                    KnowledgeIndexVersion.source_version_id == target.id
                )
            )
            assert index_version is not None
            assert index_version.status == "published"
            audit = await session.scalar(
                select(QaAdminAuditLog).where(
                    QaAdminAuditLog.resource_type == "knowledge_version",
                    QaAdminAuditLog.resource_id == str(target.id),
                    QaAdminAuditLog.action == "publish",
                )
            )
            assert audit is not None
            assert audit.actor == PUBLICATION_ACTOR
    finally:
        await _delete_actor_data(session_factory)


async def test_rollback_switches_pointer_without_changing_content(
    session_factory,
) -> None:
    try:
        async with session_factory() as session:
            async with session.begin():
                source = await _create_source(session)
                previous = await _create_version(
                    session,
                    source.id,
                    "v1",
                    "indexed",
                    checksum="previous-checksum",
                    source_uri="https://example.invalid/v1",
                )
                current = await _create_version(
                    session,
                    source.id,
                    "v2",
                    "published",
                    checksum="current-checksum",
                    source_uri="https://example.invalid/v2",
                )
                session.add(
                    KnowledgeIndexVersion(
                        source_version_id=previous.id,
                        version="v1",
                        status="indexed",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                    )
                )
                session.add(
                    KnowledgeIndexVersion(
                        source_version_id=current.id,
                        version="v2",
                        status="published",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                    )
                )
                await session.flush()

        async with session_factory() as session:
            async with session.begin():
                rolled_back = await KnowledgePublicationService().rollback(
                    session,
                    previous.id,
                    PUBLICATION_ACTOR,
                    "rollback to v1",
                )

        async with session_factory() as session:
            assert rolled_back.id == previous.id
            assert rolled_back.status == "published"
            assert rolled_back.checksum == "previous-checksum"
            current = await session.get(KnowledgeSourceVersion, current.id)
            assert current is not None
            assert current.status == "indexed"
            assert current.checksum == "current-checksum"
            assert current.source_uri == "https://example.invalid/v2"
    finally:
        await _delete_actor_data(session_factory)


async def test_rollback_requires_existing_index_pointer(session_factory) -> None:
    try:
        async with session_factory() as session:
            async with session.begin():
                source = await _create_source(session)
                target = await _create_version(
                    session,
                    source.id,
                    "v1",
                    "indexed",
                    checksum="target-checksum",
                )
                current = await _create_version(
                    session,
                    source.id,
                    "v2",
                    "published",
                    checksum="current-checksum",
                )
                session.add(
                    KnowledgeIndexVersion(
                        source_version_id=current.id,
                        version="v2",
                        status="published",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                    )
                )
                await session.flush()

        with pytest.raises(ValueError, match="index pointer"):
            async with session.begin():
                await KnowledgePublicationService().rollback(
                    session,
                    target.id,
                    PUBLICATION_ACTOR,
                    "rollback without index",
                )

        async with session_factory() as session:
            stored_target = await session.get(KnowledgeSourceVersion, target.id)
            stored_current = await session.get(
                KnowledgeSourceVersion,
                current.id,
            )
            assert stored_target is not None
            assert stored_target.status == "indexed"
            assert stored_target.checksum == "target-checksum"
            assert stored_current is not None
            assert stored_current.status == "published"
            assert stored_current.checksum == "current-checksum"
    finally:
        await _delete_actor_data(session_factory)


async def test_publish_rejects_non_indexed_version(session_factory) -> None:
    try:
        async with session_factory() as session:
            async with session.begin():
                source = await _create_source(session)
                target = await _create_version(
                    session,
                    source.id,
                    "v1",
                    "uploaded",
                )

        with pytest.raises(ValueError, match="indexed"):
            async with session.begin():
                await KnowledgePublicationService().publish(
                    session,
                    target.id,
                    PUBLICATION_ACTOR,
                    "invalid publish",
                )
    finally:
        await _delete_actor_data(session_factory)


async def _create_source(session: AsyncSession) -> KnowledgeSource:
    source = KnowledgeSource(
        source_key=f"pub-{uuid4()}",
        title="Publication Test Source",
        layer="local_authority",
        source_type="preplan",
        access_level="internal",
        created_by=PUBLICATION_ACTOR,
    )
    session.add(source)
    await session.flush()
    return source


async def _create_version(
    session: AsyncSession,
    source_id: UUID,
    version: str,
    status: str,
    *,
    checksum: str | None = None,
    source_uri: str | None = None,
) -> KnowledgeSourceVersion:
    stored = KnowledgeSourceVersion(
        source_id=source_id,
        version=version,
        status=status,
        source_uri=source_uri,
        checksum=checksum,
        created_by=PUBLICATION_ACTOR,
    )
    session.add(stored)
    await session.flush()
    return stored


async def _delete_actor_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(QaAdminAuditLog).where(
                    QaAdminAuditLog.actor == PUBLICATION_ACTOR
                )
            )
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.created_by == PUBLICATION_ACTOR
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.created_by == PUBLICATION_ACTOR
                )
            )
