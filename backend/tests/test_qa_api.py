from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.config import settings
from app.db import SessionFactory
from app.knowledge.index import RetrievedEvidence
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeSnapshot,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.retrieval import RetrievalResult
from app.main import app
from app.qa.domain import ExecutionPlan, KnowledgeQuery, ToolCallPlan
from app.qa.models import QaAnswer, QaCitation, QaQuestion, QaSession, QaToolCall
from app.qa.repository import QaRepository
from app.qa.router import get_deepseek_adapter, get_question_orchestrator
from app.qa.schemas import QaSessionCreate
from app.qa.service import AnswerEvent, QuestionOrchestrator
from app.qa.tools.registry import (
    EmptyToolInput,
    ToolExecution,
    ToolRegistry,
    ToolResult,
)

QA_API_ACTOR = "qa-api-integration"
QA_API_OTHER_ACTOR = "qa-api-other-user"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


class FakeDeepSeek:
    def __init__(
        self,
        plan: ExecutionPlan,
        *,
        chunks: list[str] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.plan_result = plan
        self.chunks = chunks or []
        self.stream_error = stream_error

    async def plan(self, question, context, tool_catalog):
        del question, context, tool_catalog
        return self.plan_result

    async def stream_answer(self, question, context, evidence, tool_results):
        del question, context, evidence, tool_results
        for chunk in self.chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


class FakeRetriever:
    def __init__(self, evidence: list[RetrievedEvidence] | None = None) -> None:
        self.evidence = evidence or []

    async def search(self, query, filters, limit=20):
        del query, filters, limit
        return RetrievalResult(
            evidence=tuple(self.evidence),
            degraded=False,
            degradation_reason=(),
        )


class StaticExecutionRegistry(ToolRegistry):
    def __init__(self, executions: list[ToolExecution]) -> None:
        super().__init__()
        self.executions = executions

    def get(self, name: str):
        if name in {execution.name for execution in self.executions}:
            return SimpleNamespace(name=name, input_model=EmptyToolInput)
        return None

    async def execute_plan(self, calls, context):
        del calls, context
        return list(self.executions)


@pytest.fixture
async def qa_db():
    ids = await _seed_knowledge_index()
    try:
        yield ids
    finally:
        await _cleanup_qa_data(ids)


@pytest.fixture
async def qa_client(qa_db):
    previous_user = app.dependency_overrides.get(get_current_user)
    previous_orchestrator = app.dependency_overrides.get(get_question_orchestrator)
    orchestrator = _orchestrator(
        chunks=["最近断裂带约 18.2 公里。 [C1]"],
    )
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=QA_API_ACTOR,
        role="viewer",
        workgroup=None,
    )
    app.dependency_overrides[get_question_orchestrator] = lambda: orchestrator
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            client.orchestrator = orchestrator
            yield client
    finally:
        _restore_override(app, get_current_user, previous_user)
        _restore_override(app, get_question_orchestrator, previous_orchestrator)


@pytest.fixture
async def default_dependency_qa_client(qa_db):
    previous_user = app.dependency_overrides.get(get_current_user)
    previous_deepseek = app.dependency_overrides.get(get_deepseek_adapter)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=QA_API_ACTOR,
        role="viewer",
        workgroup=None,
    )
    app.dependency_overrides[get_deepseek_adapter] = lambda: FakeDeepSeek(
        _plan(query="检索已发布预案依据"),
        chunks=["默认依赖检索到预案依据。 [C1]"],
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        _restore_override(app, get_deepseek_adapter, previous_deepseek)
        _restore_override(app, get_current_user, previous_user)


async def test_question_stream_has_ordered_events(qa_client) -> None:
    session = await qa_client.post(
        "/api/v1/qa/sessions",
        json={"title": "断层距离", "event_id": None},
    )
    assert session.status_code == 201

    response = await qa_client.post(
        f"/api/v1/qa/sessions/{session.json()['id']}/questions",
        json={"question": "震中距最近断裂带多少公里？"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert body.index("event: retrieval") < body.index("event: answer_completed")
    assert '"citation_key":"C1"' in body

    async with SessionFactory() as db:
        answer_id = UUID(_answer_id_from_sse(body))
        answer = await db.get(QaAnswer, answer_id)
        assert answer is not None
        assert answer.status == "completed"
        assert answer.text is not None
        assert answer.citation_keys == ["C1"]


async def test_question_uses_default_retriever_dependencies_without_orchestrator_override(
    default_dependency_qa_client,
) -> None:
    session = await default_dependency_qa_client.post(
        "/api/v1/qa/sessions",
        json={"title": "默认检索依赖", "event_id": None},
    )
    assert session.status_code == 201

    response = await default_dependency_qa_client.post(
        f"/api/v1/qa/sessions/{session.json()['id']}/questions",
        json={"question": "检索已发布预案依据"},
    )

    assert response.status_code == 200
    assert '"count":1' in response.text
    assert "retrieval_unavailable" not in response.text
    assert '"citation_key":"C1"' in response.text
    assert "默认依赖检索到预案依据" in response.text


async def test_question_requires_authentication() -> None:
    previous = app.dependency_overrides.pop(get_current_user, None)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/qa/sessions/{uuid4()}/questions",
                json={"question": "震中距最近断裂带多少公里？"},
            )
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
    assert response.status_code == 401


async def test_viewer_cannot_use_superadmin_delete(qa_client) -> None:
    answer_id = uuid4()
    response = await qa_client.delete(f"/api/v1/admin/qa/answers/{answer_id}")
    assert response.status_code == 403


async def test_create_session_locks_server_snapshot(qa_client, qa_db) -> None:
    forged_snapshot = str(uuid4())
    forged_index = str(uuid4())
    response = await qa_client.post(
        "/api/v1/qa/sessions",
        json={
            "title": "锁定快照",
            "event_id": None,
            "snapshot_id": forged_snapshot,
            "index_version_id": forged_index,
        },
    )
    assert response.status_code == 201
    assert response.json()["snapshot_id"] not in {forged_snapshot, forged_index}

    async with SessionFactory() as session:
        qa_session = await session.get(QaSession, UUID(response.json()["id"]))
        assert qa_session is not None
        assert str(qa_session.snapshot_id) == response.json()["snapshot_id"]
        assert qa_session.snapshot_id != UUID(forged_snapshot)


async def test_question_rejects_other_users_session(qa_client, session_factory) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            other_session = await QaRepository().create_session(
                session,
                AuthUser(
                    username=QA_API_OTHER_ACTOR,
                    role="viewer",
                    workgroup=None,
                ),
                QaSessionCreate(title="其他用户会话", event_id=None),
            )
        other_session_id = other_session.id

    before = await _question_count(other_session_id)
    response = await qa_client.post(
        f"/api/v1/qa/sessions/{other_session_id}/questions",
        json={"question": "越权提问"},
    )
    assert response.status_code == 403
    assert await _question_count(other_session_id) == before


async def test_get_answer_and_feedback_routes(qa_client) -> None:
    session = await qa_client.post(
        "/api/v1/qa/sessions",
        json={"title": "读取答案", "event_id": None},
    )
    response = await qa_client.post(
        f"/api/v1/qa/sessions/{session.json()['id']}/questions",
        json={"question": "震中距最近断裂带多少公里？"},
    )
    answer_id = _answer_id_from_sse(response.text)

    answer = await qa_client.get(f"/api/v1/qa/answers/{answer_id}")
    assert answer.status_code == 200
    assert answer.json()["status"] == "completed"
    assert answer.json()["citation_keys"] == ["C1"]

    feedback = await qa_client.post(
        f"/api/v1/qa/answers/{answer_id}/feedback",
        json={"helpful": True, "rating": 5, "comment": "准确"},
    )
    assert feedback.status_code == 201
    assert feedback.json()["rating"] == 5


async def test_stream_disconnect_persists_existing_text_as_partial(
    qa_db,
    session_factory,
) -> None:
    session_id = await _create_qa_session()
    executions = [_tool_execution()]
    orchestrator = _orchestrator(
        chunks=["已有文本"],
        stream_error=asyncio.CancelledError(),
        executions=executions,
    )

    answer_id = None
    with pytest.raises(asyncio.CancelledError):
        async for event in orchestrator.ask(
            "已有结论是什么？",
            session_id=session_id,
            user=_viewer(),
        ):
            if event.type == "answer_started":
                answer_id = UUID(event.data["answer_id"])

    assert answer_id is not None
    async with SessionFactory() as session:
        answer = await session.get(QaAnswer, answer_id)
        assert answer is not None
        assert answer.status == "partial"
        assert answer.text == "已有文本"
        assert await _count(session, QaToolCall, answer_id=answer_id) == 1
        assert await _count(session, QaCitation, answer_id=answer_id) == 1


async def test_stream_disconnect_without_text_is_partial(
    qa_db,
    session_factory,
) -> None:
    session_id = await _create_qa_session()
    executions = [_tool_execution()]
    orchestrator = _orchestrator(
        chunks=[],
        stream_error=asyncio.CancelledError(),
        executions=executions,
    )

    answer_id = None
    with pytest.raises(asyncio.CancelledError):
        async for event in orchestrator.ask(
            "尚无文本的问题",
            session_id=session_id,
            user=_viewer(),
        ):
            if event.type == "answer_started":
                answer_id = UUID(event.data["answer_id"])

    assert answer_id is not None
    async with SessionFactory() as session:
        answer = await session.get(QaAnswer, answer_id)
        assert answer is not None
        assert answer.status == "partial"
        assert answer.text is None
        assert await _count(session, QaToolCall, answer_id=answer_id) == 1
        assert await _count(session, QaCitation, answer_id=answer_id) == 1


async def test_stream_close_without_text_is_partial(
    qa_db,
    session_factory,
) -> None:
    session_id = await _create_qa_session()
    executions = [_tool_execution()]
    orchestrator = _orchestrator(
        chunks=[],
        executions=executions,
    )

    stream = orchestrator.ask(
        "关闭前尚无文本的问题",
        session_id=session_id,
        user=_viewer(),
    )
    answer_id = None
    async for event in stream:
        if event.type == "answer_started":
            answer_id = UUID(event.data["answer_id"])
            break
    await stream.aclose()

    assert answer_id is not None
    async with SessionFactory() as session:
        answer = await session.get(QaAnswer, answer_id)
        assert answer is not None
        assert answer.status == "partial"
        assert answer.text is None
        assert await _count(session, QaToolCall, answer_id=answer_id) == 1
        assert await _count(session, QaCitation, answer_id=answer_id) == 1


def test_format_sse_uses_compact_json_and_named_event() -> None:
    from app.qa.router import format_sse

    text = format_sse(AnswerEvent("answer_delta", {"text": "最近断裂带为..."}))
    assert text == (
        'event: answer_delta\n'
        'data: {"text":"最近断裂带为..."}\n\n'
    )


def _viewer() -> AuthUser:
    return AuthUser(username=QA_API_ACTOR, role="viewer", workgroup=None)


def _plan(
    *,
    with_tool: bool = False,
    query: str = "断层距离",
) -> ExecutionPlan:
    return ExecutionPlan(
        intent="knowledge_query",
        tool_calls=(
            [ToolCallPlan(name="fault.nearest", arguments={})]
            if with_tool
            else []
        ),
        knowledge_queries=[KnowledgeQuery(text=query, top_k=5)],
        map_intents=[],
        clarification=None,
    )


def _evidence() -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=uuid4(),
        version_id=uuid4(),
        source_title="上海地震应急预案",
        layer="local_authority",
        access_level="internal",
        text="上海市地震应急预案规定响应分级。",
        section_path=("第三章",),
        page_from=12,
        page_to=12,
        source_uri=None,
        checksum="b" * 64,
        scores={"rerank": 0.9},
    )


def _tool_execution() -> ToolExecution:
    return ToolExecution(
        name="fault.nearest",
        result=ToolResult.ok(
            value={"distance_km": 18.2},
            unit="km",
            source="shanghai.fault",
            version="v1",
            parameters={},
        ),
    )


def _orchestrator(
    *,
    chunks: list[str],
    stream_error: Exception | None = None,
    executions: list[ToolExecution] | None = None,
) -> QuestionOrchestrator:
    executions = executions or []
    return QuestionOrchestrator(
        deepseek=FakeDeepSeek(
            _plan(with_tool=bool(executions)),
            chunks=chunks,
            stream_error=stream_error,
        ),
        retriever=FakeRetriever([_evidence()]),
        registry=StaticExecutionRegistry(executions),
    )


async def _create_qa_session() -> UUID:
    async with SessionFactory() as session:
        async with session.begin():
            qa_session = await QaRepository().create_session(
                session,
                _viewer(),
                QaSessionCreate(title="流断开测试", event_id=None),
            )
            return qa_session.id


async def _seed_knowledge_index() -> dict[str, object]:
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=f"qa-api.{uuid4()}",
                title="QA API Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=QA_API_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status="published",
                checksum="a" * 64,
                created_by=QA_API_ACTOR,
            )
            session.add(version)
            await session.flush()
            index_version = KnowledgeIndexVersion(
                source_version_id=version.id,
                version="v1",
                status="published",
                collection_name=f"shanghai-knowledge-{uuid4()}",
                embedding_model=settings.embedding_model_name,
                reranker_model=settings.reranker_model_name,
                chunk_count=1,
                manifest={},
                activated_at=now,
            )
            session.add(index_version)
            await session.flush()
            session.add(
                KnowledgeChunk(
                    version_id=version.id,
                    chunk_no=1,
                    section_path=["第一章"],
                    checksum="b" * 64,
                    search_text="检索已发布预案依据 应急响应",
                    text="已发布预案依据：应急响应。",
                )
            )
            return {
                "source_id": source.id,
                "version_id": version.id,
                "index_version_id": index_version.id,
            }


async def _cleanup_qa_data(ids: dict[str, object]) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(
                delete(QaSession).where(
                    QaSession.created_by.in_(
                        [QA_API_ACTOR, QA_API_OTHER_ACTOR]
                    )
                )
            )
            await session.execute(
                delete(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.index_version_id
                    == ids["index_version_id"]
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


async def _question_count(session_id: UUID) -> int:
    async with SessionFactory() as session:
        return await _count(session, QaQuestion, session_id=session_id)


async def _count(session, model, **filters) -> int:
    statement = select(func.count()).select_from(model)
    for key, value in filters.items():
        statement = statement.where(getattr(model, key) == value)
    return await session.scalar(statement) or 0


def _answer_id_from_sse(body: str) -> str:
    match = re.search(r'"answer_id":"([0-9a-f-]+)"', body)
    assert match is not None
    return match.group(1)


def _restore_override(app, dependency, previous) -> None:
    if previous is None:
        app.dependency_overrides.pop(dependency, None)
    else:
        app.dependency_overrides[dependency] = previous
