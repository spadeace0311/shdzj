from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.main import app
from app.qa.router import get_qa_repository, get_question_orchestrator
from app.qa.schemas import (
    QaAnswerView,
    QaFeedbackResponse,
    QaSessionCreate,
    QaSessionResponse,
)
from app.qa.service import AnswerEvent


@dataclass(slots=True)
class FakeSession:
    id: UUID
    event_id: UUID | None
    snapshot_id: UUID


class FakeQaRepository:
    def __init__(self, event_id: UUID, snapshot_id: UUID) -> None:
        self.event_id = event_id
        self.snapshot_id = snapshot_id
        self.created: list[QaSessionCreate] = []
        self.deleted: list[UUID] = []
        self.feedback: list[tuple[UUID, object]] = []

    async def create_session(self, session, user, request) -> QaSessionResponse:
        del session
        self.created.append(request)
        session_id = uuid4()
        now = datetime.now(UTC)
        return QaSessionResponse(
            id=session_id,
            created_by=user.username,
            event_id=request.event_id,
            snapshot_id=self.snapshot_id,
            title=request.title,
            created_at=now,
            updated_at=now,
        )

    async def list_sessions(self, session, user, limit, cursor):
        del session, user, limit, cursor
        return []

    async def get_session(self, session, session_id, user) -> QaSessionResponse:
        del session, user
        now = datetime.now(UTC)
        return QaSessionResponse(
            id=session_id,
            created_by="viewer",
            event_id=self.event_id,
            snapshot_id=self.snapshot_id,
            title="测试会话",
            created_at=now,
            updated_at=now,
        )

    async def get_answer(self, session, answer_id, user) -> QaAnswerView:
        del session, user
        return QaAnswerView(
            id=answer_id,
            question_id=uuid4(),
            session_id=uuid4(),
            status="completed",
            text="最近断裂带约 18.2 公里。",
            structured=None,
            citation_keys=["C1"],
            degraded_reasons=[],
            duration_ms=41,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            citations=[],
            tool_calls=[],
            map_actions=[],
        )

    async def record_feedback(self, session, answer_id, user, request) -> QaFeedbackResponse:
        del session, user
        self.feedback.append((answer_id, request))
        return QaFeedbackResponse(
            id=uuid4(),
            answer_id=answer_id,
            created_by="viewer",
            helpful=request.helpful,
            rating=request.rating,
            comment=request.comment,
            created_at=datetime.now(UTC),
        )

    async def delete_answer(self, session, answer_id, actor) -> None:
        del session, actor
        self.deleted.append(answer_id)


class FakeOrchestrator:
    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        self.calls: list[tuple[str, UUID, AuthUser]] = []

    async def ask(self, question, *, session_id, user):
        self.calls.append((question, session_id, user))
        answer_id = uuid4()
        yield AnswerEvent("retrieval", {"answer_id": str(answer_id), "count": 5, "degraded": False})
        yield AnswerEvent("tool", {"name": "fault.nearest", "status": "ok", "duration_ms": 41})
        yield AnswerEvent("answer_started", {"answer_id": str(answer_id)})
        yield AnswerEvent("answer_delta", {"text": "最近断裂带约 18.2 公里。"})
        yield AnswerEvent(
            "answer_completed",
            {
                "answer_id": str(answer_id),
                "status": "completed",
                "citations": [{"citation_key": "C1"}],
            },
        )


@pytest.fixture
async def qa_client():
    event_id = uuid4()
    snapshot_id = uuid4()
    repository = FakeQaRepository(event_id, snapshot_id)
    orchestrator = FakeOrchestrator(event_id)
    previous_user = app.dependency_overrides.get(get_current_user)
    previous_repository = app.dependency_overrides.get(get_qa_repository)
    previous_orchestrator = app.dependency_overrides.get(get_question_orchestrator)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="viewer",
        role="viewer",
        workgroup=None,
    )
    app.dependency_overrides[get_qa_repository] = lambda: repository
    app.dependency_overrides[get_question_orchestrator] = lambda: orchestrator
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            client.event_id = event_id
            client.snapshot_id = snapshot_id
            client.repository = repository
            client.orchestrator = orchestrator
            yield client
    finally:
        _restore_override(app, get_current_user, previous_user)
        _restore_override(app, get_qa_repository, previous_repository)
        _restore_override(app, get_question_orchestrator, previous_orchestrator)


async def test_question_stream_has_ordered_events(qa_client) -> None:
    session = await qa_client.post(
        "/api/v1/qa/sessions",
        json={"title": "断层距离", "event_id": str(qa_client.event_id)},
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


async def test_create_session_locks_server_snapshot(qa_client) -> None:
    forged_snapshot = str(uuid4())
    forged_index = str(uuid4())
    response = await qa_client.post(
        "/api/v1/qa/sessions",
        json={
            "title": "锁定快照",
            "event_id": str(qa_client.event_id),
            "snapshot_id": forged_snapshot,
            "index_version_id": forged_index,
        },
    )
    assert response.status_code == 201
    assert response.json()["snapshot_id"] == str(qa_client.snapshot_id)
    assert response.json()["snapshot_id"] not in {forged_snapshot, forged_index}
    assert qa_client.repository.created[0].title == "锁定快照"


async def test_get_answer_and_feedback_routes(qa_client) -> None:
    answer_id = uuid4()
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
    assert qa_client.repository.feedback[0][0] == answer_id


def test_format_sse_uses_compact_json_and_named_event() -> None:
    from app.qa.router import format_sse

    text = format_sse(AnswerEvent("answer_delta", {"text": "最近断裂带为..."}))
    assert text == (
        'event: answer_delta\n'
        'data: {"text":"最近断裂带为..."}\n\n'
    )


def _restore_override(app, dependency, previous) -> None:
    if previous is None:
        app.dependency_overrides.pop(dependency, None)
    else:
        app.dependency_overrides[dependency] = previous
