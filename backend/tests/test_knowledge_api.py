from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.knowledge.domain import KnowledgeVersionStatus
from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeJob,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.main import app
from app.qa.models import QaAdminAuditLog


API_ACTOR = "knowledge-api-test"


@pytest.fixture
async def knowledge_client():
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=API_ACTOR,
        role="superadmin",
        workgroup=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_upload_creates_queued_ingest_job(
    knowledge_client,
    session_factory,
) -> None:
    try:
        source = await knowledge_client.post(
            "/api/v1/knowledge/sources",
            json={
                "source_key": "local.preplan.2026",
                "title": "上海市地震应急预案",
                "layer": "local_authority",
                "source_type": "preplan",
                "access_level": "internal",
            },
        )
        assert source.status_code == 201

        response = await knowledge_client.post(
            f"/api/v1/knowledge/sources/{source.json()['id']}/versions",
            data={"version": "2026.1", "source_uri": "upload://preplan.docx"},
            files={
                "file": (
                    "preplan.docx",
                    b"docx-test-bytes",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "uploaded"

        jobs = await knowledge_client.get("/api/v1/knowledge/jobs")
        assert jobs.status_code == 200
        assert jobs.json()[0]["job_type"] == "ingest"
        assert jobs.json()[0]["status"] == "queued"
    finally:
        await _delete_actor_data(session_factory)


async def test_url_version_route_queues_fetch_job(
    knowledge_client,
    session_factory,
) -> None:
    try:
        source = await knowledge_client.post(
            "/api/v1/knowledge/sources",
            json={
                "source_key": f"local.url.{uuid4()}",
                "title": "上海市公开知识网页",
                "layer": "public_reference",
                "source_type": "web",
                "access_level": "public",
            },
        )
        assert source.status_code == 201

        response = await knowledge_client.post(
            (
                "/api/v1/knowledge/sources/"
                f"{source.json()['id']}/url-versions"
            ),
            json={
                "version": "2026.1",
                "source_uri": "https://www.sh.gov.cn/example.html",
                "metadata": {"topic": "preplan"},
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "registered"

        jobs = await knowledge_client.get("/api/v1/knowledge/jobs")
        assert jobs.status_code == 200
        assert jobs.json()[0]["job_type"] == "fetch"
        assert jobs.json()[0]["status"] == "queued"
        assert jobs.json()[0]["request_payload"]["source_uri"] == (
            "https://www.sh.gov.cn/example.html"
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_viewer_cannot_write(
    knowledge_client,
    session_factory,
) -> None:
    try:
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: AuthUser(
            username="viewer-user",
            role="viewer",
            workgroup=None,
        )
        try:
            response = await knowledge_client.post(
                "/api/v1/knowledge/sources",
                json={
                    "source_key": f"local.viewer.{uuid4()}",
                    "title": "Viewer Cannot Create",
                    "layer": "local_authority",
                    "source_type": "preplan",
                    "access_level": "internal",
                },
            )
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_current_user, None)
            else:
                app.dependency_overrides[get_current_user] = previous

        assert response.status_code == 403
    finally:
        await _delete_actor_data(session_factory)


async def test_publish_and_rollback_routes(
    knowledge_client,
    session_factory,
) -> None:
    try:
        version_ids = await _seed_indexed_versions(session_factory)
        previous_id, current_id = version_ids

        published = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{current_id}/publish",
            json={"reason": "release current"},
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"

        rolled_back = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{previous_id}/rollback",
            json={"reason": "rollback previous"},
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["status"] == "published"

        audit = await _audit_resource(session_factory, previous_id, "rollback")
        assert audit is not None
    finally:
        await _delete_actor_data(session_factory)


async def test_rebuild_version_route_queues_forced_index_job(
    knowledge_client,
    session_factory,
) -> None:
    try:
        _previous_id, version_id = await _seed_indexed_versions(session_factory)

        response = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/rebuild",
            json={"reason": "restore empty qdrant collection"},
        )

        assert response.status_code == 202
        assert response.json()["job_type"] == "index"
        assert response.json()["status"] == "queued"
        assert response.json()["request_payload"]["force"] is True
        assert response.json()["request_payload"]["reason"] == (
            "restore empty qdrant collection"
        )
    finally:
        await _delete_actor_data(session_factory)


async def test_rebuild_version_route_reuses_pending_forced_job(
    knowledge_client,
    session_factory,
) -> None:
    try:
        _previous_id, version_id = await _seed_indexed_versions(session_factory)

        first = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/rebuild",
            json={"reason": "first rebuild request"},
        )
        second = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/rebuild",
            json={"reason": "duplicate rebuild request"},
        )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["id"] == second.json()["id"]

        async with session_factory() as session:
            jobs = list(
                (
                    await session.scalars(
                        select(KnowledgeJob).where(
                            KnowledgeJob.version_id == version_id,
                            KnowledgeJob.job_type == "index",
                        )
                    )
                ).all()
            )
        assert len(jobs) == 1
        assert jobs[0].request_payload["force"] is True
    finally:
        await _delete_actor_data(session_factory)


async def test_disable_publish_enable_lifecycle(
    knowledge_client,
    session_factory,
) -> None:
    try:
        _previous_id, version_id = await _seed_indexed_versions(session_factory)

        disabled = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/disable",
            json={"reason": "incident review"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == KnowledgeVersionStatus.DISABLED.value

        publish_disabled = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish",
            json={"reason": "must be rejected"},
        )
        assert publish_disabled.status_code == 409

        enabled = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/enable",
            json={"reason": "review completed"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == KnowledgeVersionStatus.INDEXED.value

        published = await knowledge_client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish",
            json={"reason": "release again"},
        )
        assert published.status_code == 200
        assert published.json()["status"] == KnowledgeVersionStatus.PUBLISHED.value
    finally:
        await _delete_actor_data(session_factory)


async def test_retry_job_requeues_failed_job(
    knowledge_client,
    session_factory,
) -> None:
    job_id = await _seed_failed_job(session_factory)
    try:
        response = await knowledge_client.post(
            f"/api/v1/knowledge/jobs/{job_id}/retry"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
    finally:
        await _delete_actor_data(session_factory)


async def test_retry_job_resets_dead_letter_attempt_count(
    knowledge_client,
    session_factory,
) -> None:
    job_id = await _seed_dead_letter_job(session_factory)
    try:
        response = await knowledge_client.post(
            f"/api/v1/knowledge/jobs/{job_id}/retry"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert response.json()["attempt_count"] == 0

        async with session_factory() as session:
            job = await session.get(KnowledgeJob, job_id)
            assert job is not None
            assert job.status == "queued"
            assert job.attempt_count == 0
    finally:
        await _delete_actor_data(session_factory)


async def _seed_indexed_versions(session_factory):
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"local.api.{uuid4()}",
                title="API Publication Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=API_ACTOR,
            )
            session.add(source)
            await session.flush()

            previous = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status="indexed",
                checksum="a" * 64,
                created_by=API_ACTOR,
            )
            current = KnowledgeSourceVersion(
                source_id=source.id,
                version="v2",
                status="indexed",
                checksum="b" * 64,
                created_by=API_ACTOR,
            )
            session.add_all([previous, current])
            await session.flush()
            session.add_all(
                [
                    KnowledgeIndexVersion(
                        source_version_id=previous.id,
                        version="v1",
                        status="indexed",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                        chunk_count=1,
                    ),
                    KnowledgeIndexVersion(
                        source_version_id=current.id,
                        version="v2",
                        status="indexed",
                        collection_name="shanghai-knowledge-source",
                        embedding_model="BAAI/bge-m3",
                        reranker_model="BAAI/bge-reranker-v2-m3",
                        chunk_count=1,
                    ),
                ]
            )
            await session.flush()
            return previous.id, current.id


async def _seed_failed_job(session_factory):
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"local.retry.{uuid4()}",
                title="API Retry Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=API_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status="failed",
                created_by=API_ACTOR,
            )
            session.add(version)
            await session.flush()
            job = KnowledgeJob(
                version_id=version.id,
                job_type="ingest",
                status="failed",
                last_error="synthetic failure",
            )
            session.add(job)
            await session.flush()
            return job.id


async def _seed_dead_letter_job(session_factory):
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"local.dead-letter.{uuid4()}",
                title="API Dead Letter Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=API_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status="failed",
                created_by=API_ACTOR,
            )
            session.add(version)
            await session.flush()
            job = KnowledgeJob(
                version_id=version.id,
                job_type="ingest",
                status="dead_letter",
                attempt_count=5,
                max_attempts=5,
                last_error="retry budget exhausted",
            )
            session.add(job)
            await session.flush()
            return job.id


async def _audit_resource(session_factory, version_id, action):
    async with session_factory() as session:
        return await session.scalar(
            select(QaAdminAuditLog).where(
                QaAdminAuditLog.resource_type == "knowledge_version",
                QaAdminAuditLog.resource_id == str(version_id),
                QaAdminAuditLog.action == action,
            )
        )


async def _delete_actor_data(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(QaAdminAuditLog).where(QaAdminAuditLog.actor == API_ACTOR)
            )
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.created_by == API_ACTOR
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.created_by == API_ACTOR
                )
            )
