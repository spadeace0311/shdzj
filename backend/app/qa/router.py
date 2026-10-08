from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import get_current_user, require_role
from app.auth.service import AuthUser
from app.db import get_session
from app.qa.repository import (
    KnowledgeVersionPurgeService,
    QaAdminPurgeService,
    QaRepository,
    _session_response,
)
from app.qa.schemas import (
    OperationLogPurgeFilters,
    OperationLogPurgeResponse,
    QaAnswerView,
    QaFeedbackCreate,
    QaFeedbackResponse,
    QaQuestionCreate,
    QaSessionCreate,
    QaSessionResponse,
)
from app.qa.service import AnswerEvent, QuestionOrchestrator

router = APIRouter(prefix="/api/v1", tags=["qa"])


def get_qa_repository() -> QaRepository:
    return QaRepository()


def get_question_orchestrator() -> QuestionOrchestrator:
    return QuestionOrchestrator()


def get_qa_admin_purge_service() -> QaAdminPurgeService:
    return QaAdminPurgeService()


def get_knowledge_version_purge_service() -> KnowledgeVersionPurgeService:
    return KnowledgeVersionPurgeService()


def format_sse(event: AnswerEvent) -> str:
    payload = json.dumps(
        event.data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.type}\ndata: {payload}\n\n"


@router.post(
    "/qa/sessions",
    response_model=QaSessionResponse,
    status_code=201,
)
async def create_qa_session(
    request: QaSessionCreate,
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(get_current_user),
) -> QaSessionResponse:
    try:
        async with session.begin():
            qa_session = await repository.create_session(
                session,
                current_user,
                request,
            )
        return _session_response(qa_session)
    except (LookupError, PermissionError, IntegrityError, ValueError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


@router.get(
    "/qa/sessions",
    response_model=list[QaSessionResponse],
)
async def list_qa_sessions(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(get_current_user),
) -> list[QaSessionResponse]:
    try:
        rows = await repository.list_sessions(
            session,
            current_user,
            limit,
            cursor,
        )
        return [_session_response(row) for row in rows]
    except (LookupError, PermissionError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


@router.get(
    "/qa/sessions/{session_id}",
    response_model=QaSessionResponse,
)
async def get_qa_session(
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(get_current_user),
) -> QaSessionResponse:
    try:
        qa_session = await repository.get_session(
            session,
            session_id,
            current_user,
        )
        return _session_response(qa_session)
    except (LookupError, PermissionError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


@router.post(
    "/qa/sessions/{session_id}/questions",
    response_class=StreamingResponse,
)
async def ask_qa_question(
    session_id: UUID,
    request: QaQuestionCreate,
    orchestrator: QuestionOrchestrator = Depends(get_question_orchestrator),
    current_user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    async def event_stream():
        try:
            async for event in orchestrator.ask(
                request.question,
                session_id=session_id,
                user=current_user,
            ):
                yield format_sse(event)
        finally:
            # The orchestrator's own finally marks interrupted answers partial.
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/qa/answers/{answer_id}",
    response_model=QaAnswerView,
)
async def get_qa_answer(
    answer_id: UUID,
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(get_current_user),
) -> QaAnswerView:
    try:
        return await repository.get_answer(
            session,
            answer_id,
            current_user,
        )
    except (LookupError, PermissionError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


@router.post(
    "/qa/answers/{answer_id}/feedback",
    response_model=QaFeedbackResponse,
    status_code=201,
)
async def record_qa_feedback(
    answer_id: UUID,
    request: QaFeedbackCreate,
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(get_current_user),
) -> QaFeedbackResponse:
    try:
        async with session.begin():
            feedback = await repository.record_feedback(
                session,
                answer_id,
                current_user,
                request,
            )
        return _feedback_response(feedback)
    except (LookupError, PermissionError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


@router.delete(
    "/admin/qa/answers/{answer_id}",
    status_code=204,
)
async def delete_qa_answer(
    answer_id: UUID,
    session: AsyncSession = Depends(get_session),
    repository: QaRepository = Depends(get_qa_repository),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> Response:
    try:
        async with session.begin():
            await repository.delete_answer(
                session,
                answer_id,
                current_user.username,
            )
    except (LookupError, PermissionError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc
    return Response(status_code=204)


@router.delete(
    "/admin/knowledge/versions/{version_id}",
    status_code=204,
)
async def delete_knowledge_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeVersionPurgeService = Depends(
        get_knowledge_version_purge_service
    ),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> Response:
    try:
        async with session.begin():
            await service.delete_version(
                session,
                version_id,
                current_user.username,
            )
    except (LookupError, PermissionError, IntegrityError, ValueError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc
    return Response(status_code=204)


@router.delete(
    "/admin/qa/operation-logs",
    response_model=OperationLogPurgeResponse,
)
async def purge_qa_operation_logs(
    event_id: UUID | None = Query(default=None),
    answer_id: UUID | None = Query(default=None),
    actor: str | None = Query(default=None),
    before: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    service: QaAdminPurgeService = Depends(get_qa_admin_purge_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> OperationLogPurgeResponse:
    filters = OperationLogPurgeFilters(
        event_id=event_id,
        answer_id=answer_id,
        actor=actor,
        before=before,
    )
    try:
        async with session.begin():
            deleted = await service.purge_operation_logs(
                session,
                current_user.username,
                filters,
            )
        return OperationLogPurgeResponse(deleted=deleted)
    except (IntegrityError, SQLAlchemyError) as exc:
        raise _map_qa_error(exc) from exc


def _feedback_response(feedback) -> QaFeedbackResponse:
    return QaFeedbackResponse(
        id=feedback.id,
        answer_id=feedback.answer_id,
        created_by=feedback.created_by,
        helpful=feedback.helpful,
        rating=feedback.rating,
        comment=feedback.comment,
        created_at=feedback.created_at,
    )


def _map_qa_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail="Insufficient permissions")
    if isinstance(error, IntegrityError):
        return HTTPException(status_code=409, detail="qa resource conflict")
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SQLAlchemyError):
        return HTTPException(status_code=503, detail="qa storage is unavailable")
    raise error
