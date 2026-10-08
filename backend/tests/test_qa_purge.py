from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.config import settings
from app.db import SessionFactory
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSnapshot,
    KnowledgeSource,
    KnowledgeSourceVersion,
    KnowledgeWebSnapshot,
)
from app.main import app
from app.qa.models import (
    QaAdminAuditLog,
    QaAnswer,
    QaCitation,
    QaFeedback,
    QaMapAction,
    QaQuestion,
    QaSession,
    QaToolCall,
)
from app.qa.repository import (
    KnowledgeVersionPurgeService,
    QaAdminPurgeService,
    QaRepository,
)
from app.qa.router import (
    get_knowledge_version_purge_service,
    get_qa_admin_purge_service,
    get_qa_repository,
)
from app.qa.schemas import OperationLogPurgeFilters

QA_PURGE_ACTOR = "qa-purge-integration"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


class RecordingKnowledgeIndex:
    def __init__(self) -> None:
        self.deletes: list[tuple[str, object]] = []

    async def delete_version(self, index_version, version_id) -> None:
        self.deletes.append((index_version.collection_name, version_id))


class FakeQaRepository:
    def __init__(self) -> None:
        self.deleted: list[object] = []

    async def delete_answer(self, session, answer_id, actor) -> None:
        del session
        self.deleted.append((answer_id, actor))


class FakeQaAdminPurgeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def purge_operation_logs(self, session, actor, filters) -> int:
        del session
        self.calls.append((actor, filters))
        return 3


class FakeKnowledgeVersionPurgeService:
    def __init__(self) -> None:
        self.deleted: list[tuple[object, str]] = []

    async def delete_version(self, session, version_id, actor) -> None:
        del session
        self.deleted.append((version_id, actor))


@pytest.fixture
async def purge_client():
    repository = FakeQaRepository()
    qa_purge = FakeQaAdminPurgeService()
    version_purge = FakeKnowledgeVersionPurgeService()
    overrides = {
        get_current_user: lambda: AuthUser(
            username="superadmin",
            role="superadmin",
            workgroup=None,
        ),
        get_qa_repository: lambda: repository,
        get_qa_admin_purge_service: lambda: qa_purge,
        get_knowledge_version_purge_service: lambda: version_purge,
    }
    previous = {
        dependency: app.dependency_overrides.get(dependency)
        for dependency in overrides
    }
    for dependency, value in overrides.items():
        app.dependency_overrides[dependency] = value
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            client.repository = repository
            client.qa_purge = qa_purge
            client.version_purge = version_purge
            yield client
    finally:
        for dependency, value in previous.items():
            if value is None:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = value


async def test_superadmin_can_delete_qa_answer(purge_client) -> None:
    answer_id = uuid4()
    response = await purge_client.delete(f"/api/v1/admin/qa/answers/{answer_id}")
    assert response.status_code == 204
    assert purge_client.repository.deleted == [(answer_id, "superadmin")]


async def test_superadmin_can_delete_knowledge_version(purge_client) -> None:
    version_id = uuid4()
    response = await purge_client.delete(
        f"/api/v1/admin/knowledge/versions/{version_id}"
    )
    assert response.status_code == 204
    assert purge_client.version_purge.deleted == [(version_id, "superadmin")]


async def test_superadmin_can_purge_operation_logs_with_filters(purge_client) -> None:
    event_id = uuid4()
    answer_id = uuid4()
    before = datetime.now(UTC).isoformat()
    response = await purge_client.delete(
        "/api/v1/admin/qa/operation-logs",
        params={
            "event_id": str(event_id),
            "answer_id": str(answer_id),
            "actor": "alice",
            "before": before,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": 3}
    actor, filters = purge_client.qa_purge.calls[0]
    assert actor == "superadmin"
    assert str(filters.event_id) == str(event_id)
    assert str(filters.answer_id) == str(answer_id)
    assert filters.actor == "alice"
    assert filters.before.isoformat() == before


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/admin/qa/answers/{uuid4()}",
        f"/api/v1/admin/knowledge/versions/{uuid4()}",
        "/api/v1/admin/qa/operation-logs",
    ],
)
async def test_viewer_cannot_call_admin_purge_routes(path: str) -> None:
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="viewer",
        role="viewer",
        workgroup=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.delete(path)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
    assert response.status_code == 403


async def test_delete_answer_cascades_children_and_writes_audit(session_factory) -> None:
    ids = await _seed_answer_fixture(session_factory)
    try:
        async with SessionFactory() as session:
            async with session.begin():
                await QaRepository().delete_answer(
                    session,
                    ids["answer_id"],
                    QA_PURGE_ACTOR,
                )

        async with SessionFactory() as session:
            assert await session.get(QaAnswer, ids["answer_id"]) is None
            assert await session.get(QaQuestion, ids["question_id"]) is not None
            assert await _count(session, QaCitation, answer_id=ids["answer_id"]) == 0
            assert await _count(session, QaToolCall, answer_id=ids["answer_id"]) == 0
            assert await _count(session, QaMapAction, answer_id=ids["answer_id"]) == 0
            assert await _count(session, QaFeedback, answer_id=ids["answer_id"]) == 0
            audit = await session.scalar(
                select(QaAdminAuditLog).where(
                    QaAdminAuditLog.resource_type == "qa_answer",
                    QaAdminAuditLog.resource_id == str(ids["answer_id"]),
                    QaAdminAuditLog.action == "delete",
                )
            )
            assert audit is not None
            assert audit.answer_id == ids["answer_id"]
    finally:
        await _cleanup_session_fixture(session_factory, ids)


async def test_purge_operation_logs_preserves_new_audit(session_factory) -> None:
    answer_id = uuid4()
    event_id = uuid4()
    async with SessionFactory() as session:
        async with session.begin():
            session.add_all(
                [
                    QaAdminAuditLog(
                        resource_type="qa_answer",
                        resource_id=str(uuid4()),
                        action="delete",
                        actor="alice",
                        event_id=event_id,
                        answer_id=answer_id,
                        details={},
                    ),
                    QaAdminAuditLog(
                        resource_type="knowledge_version",
                        resource_id=str(uuid4()),
                        action="delete",
                        actor="alice",
                        details={},
                    ),
                ]
            )

    try:
        async with SessionFactory() as session:
            async with session.begin():
                deleted = await QaAdminPurgeService().purge_operation_logs(
                    session,
                    "alice",
                    OperationLogPurgeFilters(
                        event_id=event_id,
                        answer_id=answer_id,
                        actor="alice",
                    ),
                )
        assert deleted == 1

        async with SessionFactory() as session:
            remaining = await _count(
                session,
                QaAdminAuditLog,
                actor="alice",
                answer_id=answer_id,
            )
            assert remaining == 0
            purge_audit = await session.scalar(
                select(QaAdminAuditLog).where(
                    QaAdminAuditLog.resource_type == "qa_operation_log",
                    QaAdminAuditLog.action == "purge",
                    QaAdminAuditLog.actor == "alice",
                )
            )
            assert purge_audit is not None
    finally:
        await _delete_qa_audit_rows(session_factory, "alice")


async def test_delete_knowledge_version_deletes_vector_points_before_rows(
    session_factory,
) -> None:
    ids = await _seed_version_fixture(session_factory)
    index = RecordingKnowledgeIndex()
    try:
        async with SessionFactory() as session:
            async with session.begin():
                await KnowledgeVersionPurgeService(index=index).delete_version(
                    session,
                    ids["version_id"],
                    QA_PURGE_ACTOR,
                )

        assert [entry[1] for entry in index.deletes] == [ids["version_id"]]
        async with SessionFactory() as session:
            assert (
                await session.get(KnowledgeSourceVersion, ids["version_id"])
                is None
            )
            assert await session.get(KnowledgeChunk, ids["chunk_id"]) is None
            assert (
                await session.get(KnowledgeWebSnapshot, ids["web_snapshot_id"])
                is None
            )
            assert await session.get(KnowledgeJob, ids["job_id"]) is None
            audit = await session.scalar(
                select(QaAdminAuditLog).where(
                    QaAdminAuditLog.resource_type == "knowledge_version",
                    QaAdminAuditLog.resource_id == str(ids["version_id"]),
                    QaAdminAuditLog.action == "delete",
                )
            )
            assert audit is not None
    finally:
        await _cleanup_source_fixture(session_factory, ids["source_id"])
        await _delete_qa_audit_rows(session_factory, QA_PURGE_ACTOR)


async def test_delete_knowledge_version_rejects_snapshot_reference(
    session_factory,
) -> None:
    ids = await _seed_version_fixture(session_factory, with_snapshot=True)
    index = RecordingKnowledgeIndex()
    try:
        async with SessionFactory() as session:
            async with session.begin():
                with pytest.raises(ValueError, match="snapshot"):
                    await KnowledgeVersionPurgeService(index=index).delete_version(
                        session,
                        ids["version_id"],
                        QA_PURGE_ACTOR,
                    )
        assert index.deletes == []
        async with SessionFactory() as session:
            assert (
                await session.get(KnowledgeSourceVersion, ids["version_id"])
                is not None
            )
    finally:
        await _cleanup_version_fixture(session_factory, ids)
        await _delete_qa_audit_rows(session_factory, QA_PURGE_ACTOR)


async def _seed_answer_fixture(session_factory) -> dict[str, object]:
    version_ids = await _seed_version_fixture(
        session_factory,
        with_snapshot=True,
    )
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            qa_session = QaSession(
                created_by=QA_PURGE_ACTOR,
                event_id=None,
                snapshot_id=version_ids["snapshot_id"],
                title="purge answer fixture",
                created_at=now,
                updated_at=now,
            )
            session.add(qa_session)
            await session.flush()
            question = QaQuestion(
                session_id=qa_session.id,
                question_text="purge fixture",
                status="completed",
                created_at=now,
            )
            session.add(question)
            await session.flush()
            answer = QaAnswer(
                question_id=question.id,
                status="completed",
                text="fixture answer",
                structured={},
                citation_keys=["C1"],
                degraded_reasons=[],
                duration_ms=1,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
            session.add(answer)
            await session.flush()
            session.add_all(
                [
                    QaCitation(
                        answer_id=answer.id,
                        citation_key="C1",
                        source_title="fixture source",
                        version_label="v1",
                        excerpt="fixture excerpt",
                        checksum="a" * 64,
                    ),
                    QaToolCall(
                        answer_id=answer.id,
                        tool_name="fault.nearest",
                        tool_status="ok",
                        arguments={},
                        result={},
                        limitations=[],
                        duration_ms=1,
                    ),
                    QaMapAction(
                        answer_id=answer.id,
                        action_type="locate",
                        payload={},
                    ),
                    QaFeedback(
                        answer_id=answer.id,
                        created_by=QA_PURGE_ACTOR,
                        rating=5,
                    ),
                ]
            )
            await session.flush()
            return {
                **version_ids,
                "session_id": qa_session.id,
                "question_id": question.id,
                "answer_id": answer.id,
            }


async def _seed_version_fixture(
    session_factory,
    *,
    with_snapshot: bool = False,
) -> dict[str, object]:
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"qa-purge.{uuid4()}",
                title="QA Purge Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=QA_PURGE_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version=f"v-{uuid4()}",
                status="indexed",
                checksum="b" * 64,
                created_by=QA_PURGE_ACTOR,
            )
            session.add(version)
            await session.flush()
            index_version = KnowledgeIndexVersion(
                source_version_id=version.id,
                version=version.version,
                status="published",
                collection_name=f"shanghai-knowledge-{uuid4()}",
                embedding_model=settings.embedding_model_name,
                reranker_model=settings.reranker_model_name,
                manifest={},
                activated_at=now,
            )
            session.add(index_version)
            await session.flush()
            snapshot_id = None
            if with_snapshot:
                snapshot = KnowledgeSnapshot(
                    event_id=None,
                    index_version_id=index_version.id,
                    manifest={},
                    fingerprint=uuid4().hex + uuid4().hex,
                    created_at=now,
                )
                session.add(snapshot)
                await session.flush()
                snapshot_id = snapshot.id
            chunk = KnowledgeChunk(
                version_id=version.id,
                chunk_no=1,
                checksum="c" * 64,
                search_text="fixture",
                text="fixture chunk",
            )
            web_snapshot = KnowledgeWebSnapshot(
                version_id=version.id,
                requested_url="https://example.invalid/fixture",
            )
            job = KnowledgeJob(
                version_id=version.id,
                job_type="ingest",
                status="succeeded",
            )
            session.add_all([chunk, web_snapshot, job])
            await session.flush()
            return {
                "source_id": source.id,
                "version_id": version.id,
                "index_version_id": index_version.id,
                "snapshot_id": snapshot_id,
                "chunk_id": chunk.id,
                "web_snapshot_id": web_snapshot.id,
                "job_id": job.id,
            }


async def _count(session, model, **filters) -> int:
    statement = select(func.count()).select_from(model)
    for key, value in filters.items():
        statement = statement.where(getattr(model, key) == value)
    return await session.scalar(statement) or 0


async def _cleanup_session_fixture(session_factory, ids) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(
                delete(QaSession).where(QaSession.id == ids["session_id"])
            )
            await session.execute(
                delete(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.id == ids["snapshot_id"]
                )
            )
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.id == ids["version_id"]
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.id == ids["source_id"]
                )
            )
    await _delete_qa_audit_rows(session_factory, QA_PURGE_ACTOR)


async def _cleanup_version_fixture(session_factory, ids) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            if ids.get("snapshot_id") is not None:
                await session.execute(
                    delete(KnowledgeSnapshot).where(
                        KnowledgeSnapshot.id == ids["snapshot_id"]
                    )
                )
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.id == ids["version_id"]
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.id == ids["source_id"]
                )
            )


async def _cleanup_source_fixture(session_factory, source_id) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(
                delete(KnowledgeSource).where(KnowledgeSource.id == source_id)
            )


async def _delete_qa_audit_rows(session_factory, actor: str) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(
                delete(QaAdminAuditLog).where(QaAdminAuditLog.actor == actor)
            )
