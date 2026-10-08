from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthUser
from app.knowledge.index import KnowledgeIndex
from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeSnapshot,
    KnowledgeSourceVersion,
)
from app.knowledge.snapshot import (
    KnowledgeSnapshotService,
    locked_index_version_ids,
)
from app.qa.access import AccessPolicy
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
from app.qa.schemas import (
    OperationLogPurgeFilters,
    QaAnswerView,
    QaCitationResponse,
    QaFeedbackCreate,
    QaMapActionResponse,
    QaSessionCreate,
    QaSessionResponse,
    QaToolCallResponse,
)


class QaRepository:
    def __init__(
        self,
        *,
        snapshot_service: KnowledgeSnapshotService | None = None,
        access_policy: AccessPolicy | None = None,
    ) -> None:
        self._snapshot_service = snapshot_service or KnowledgeSnapshotService()
        self._access = access_policy or AccessPolicy()

    async def create_session(
        self,
        session: AsyncSession,
        user: AuthUser,
        request: QaSessionCreate,
    ) -> QaSession:
        snapshot = await self._snapshot_service.create(
            session,
            event_id=request.event_id,
            created_by=user.username,
        )
        now = datetime.now(UTC)
        qa_session = QaSession(
            created_by=user.username,
            event_id=request.event_id,
            snapshot_id=snapshot.id,
            title=request.title,
            created_at=now,
            updated_at=now,
        )
        session.add(qa_session)
        await session.flush()
        return qa_session

    async def list_sessions(
        self,
        session: AsyncSession,
        user: AuthUser,
        limit: int,
        cursor: UUID | None,
    ) -> list[QaSession]:
        statement = select(QaSession)
        if user.role != "superadmin":
            statement = statement.where(QaSession.created_by == user.username)
        if cursor is not None:
            cursor_row = await session.get(QaSession, cursor)
            if cursor_row is None:
                return []
            statement = statement.where(
                (QaSession.updated_at < cursor_row.updated_at)
                | (
                    (QaSession.updated_at == cursor_row.updated_at)
                    & (QaSession.id < cursor_row.id)
                )
            )
        statement = statement.order_by(
            QaSession.updated_at.desc(),
            QaSession.id.desc(),
        ).limit(limit)
        return list((await session.scalars(statement)).all())

    async def get_session(
        self,
        session: AsyncSession,
        session_id: UUID,
        user: AuthUser,
    ) -> QaSession:
        qa_session = await session.get(QaSession, session_id)
        if qa_session is None:
            raise LookupError("qa session not found")
        self._ensure_session_access(qa_session, user)
        return qa_session

    async def get_answer(
        self,
        session: AsyncSession,
        answer_id: UUID,
        user: AuthUser,
    ) -> QaAnswerView:
        answer, _question, qa_session = await self._load_answer(
            session,
            answer_id,
        )
        self._ensure_session_access(qa_session, user)
        if not self._access.can_read_event(user, qa_session.event_id):
            raise PermissionError("Insufficient permissions")
        citations = await self._list_citations(session, answer.id)
        tool_calls = await self._list_tool_calls(session, answer.id)
        map_actions = await self._list_map_actions(session, answer.id)
        return QaAnswerView(
            id=answer.id,
            question_id=answer.question_id,
            session_id=qa_session.id,
            status=answer.status,
            text=answer.text,
            structured=answer.structured,
            citation_keys=list(answer.citation_keys or []),
            degraded_reasons=list(answer.degraded_reasons or []),
            duration_ms=answer.duration_ms,
            created_at=answer.created_at or datetime.now(UTC),
            updated_at=answer.updated_at or datetime.now(UTC),
            completed_at=answer.completed_at,
            citations=citations,
            tool_calls=tool_calls,
            map_actions=map_actions,
        )

    async def record_feedback(
        self,
        session: AsyncSession,
        answer_id: UUID,
        user: AuthUser,
        request: QaFeedbackCreate,
    ) -> QaFeedback:
        answer, _question, qa_session = await self._load_answer(
            session,
            answer_id,
        )
        self._ensure_session_access(qa_session, user)
        if not self._access.can_read_event(user, qa_session.event_id):
            raise PermissionError("Insufficient permissions")
        feedback = QaFeedback(
            answer_id=answer.id,
            created_by=user.username,
            helpful=request.helpful,
            rating=request.rating,
            comment=request.comment,
            created_at=datetime.now(UTC),
        )
        session.add(feedback)
        await session.flush()
        return feedback

    async def delete_answer(
        self,
        session: AsyncSession,
        answer_id: UUID,
        actor: str,
    ) -> None:
        answer, _question, qa_session = await self._load_answer(session, answer_id)
        session.add(
            QaAdminAuditLog(
                resource_type="qa_answer",
                resource_id=str(answer.id),
                action="delete",
                actor=actor,
                event_id=qa_session.event_id,
                answer_id=answer.id,
                details={"status": answer.status},
            )
        )
        await session.delete(answer)
        await session.flush()

    async def _load_answer(
        self,
        session: AsyncSession,
        answer_id: UUID,
    ) -> tuple[QaAnswer, QaQuestion, QaSession]:
        answer = await session.get(QaAnswer, answer_id)
        if answer is None:
            raise LookupError("qa answer not found")
        question = await session.get(QaQuestion, answer.question_id)
        if question is None:
            raise LookupError("qa question not found")
        qa_session = await session.get(QaSession, question.session_id)
        if qa_session is None:
            raise LookupError("qa session not found")
        return answer, question, qa_session

    async def _list_citations(
        self,
        session: AsyncSession,
        answer_id: UUID,
    ) -> list[QaCitationResponse]:
        rows = list(
            (
                await session.scalars(
                    select(QaCitation)
                    .where(QaCitation.answer_id == answer_id)
                    .order_by(QaCitation.citation_key, QaCitation.id)
                )
            ).all()
        )
        return [_citation_response(row) for row in rows]

    async def _list_tool_calls(
        self,
        session: AsyncSession,
        answer_id: UUID,
    ) -> list[QaToolCallResponse]:
        rows = list(
            (
                await session.scalars(
                    select(QaToolCall)
                    .where(QaToolCall.answer_id == answer_id)
                    .order_by(QaToolCall.created_at, QaToolCall.id)
                )
            ).all()
        )
        return [_tool_call_response(row) for row in rows]

    async def _list_map_actions(
        self,
        session: AsyncSession,
        answer_id: UUID,
    ) -> list[QaMapActionResponse]:
        rows = list(
            (
                await session.scalars(
                    select(QaMapAction)
                    .where(QaMapAction.answer_id == answer_id)
                    .order_by(QaMapAction.created_at, QaMapAction.id)
                )
            ).all()
        )
        return [_map_action_response(row) for row in rows]

    @staticmethod
    def _ensure_session_access(qa_session: QaSession, user: AuthUser) -> None:
        if user.role != "superadmin" and qa_session.created_by != user.username:
            raise PermissionError("Insufficient permissions")


class QaAdminPurgeService:
    async def purge_operation_logs(
        self,
        session: AsyncSession,
        actor: str,
        filters: OperationLogPurgeFilters,
    ) -> int:
        audit = QaAdminAuditLog(
            resource_type="qa_operation_log",
            resource_id="batch",
            action="purge",
            actor=actor,
            details=filters.model_dump(mode="json"),
        )
        session.add(audit)
        await session.flush()

        statement = delete(QaAdminAuditLog).where(QaAdminAuditLog.id != audit.id)
        if filters.event_id is not None:
            statement = statement.where(QaAdminAuditLog.event_id == filters.event_id)
        if filters.answer_id is not None:
            statement = statement.where(QaAdminAuditLog.answer_id == filters.answer_id)
        if filters.actor is not None:
            statement = statement.where(QaAdminAuditLog.actor == filters.actor)
        if filters.before is not None:
            statement = statement.where(QaAdminAuditLog.created_at < filters.before)
        result = await session.execute(statement)
        return result.rowcount or 0


class KnowledgeVersionPurgeService:
    def __init__(self, *, index: KnowledgeIndex | None = None) -> None:
        self._index = index or KnowledgeIndex()
        self._owns_index = index is None

    async def delete_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
    ) -> None:
        try:
            await self._delete_version(session, version_id, actor)
        finally:
            if self._owns_index:
                await self._index.close()

    async def _delete_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
    ) -> None:
        version = await session.get(
            KnowledgeSourceVersion,
            version_id,
            with_for_update=True,
        )
        if version is None:
            raise LookupError("knowledge source version not found")

        referenced = await session.scalar(
            select(KnowledgeSnapshot.id)
            .where(
                KnowledgeSnapshot.index_version_id.in_(
                    select(KnowledgeIndexVersion.id).where(
                        KnowledgeIndexVersion.source_version_id == version_id
                    )
                )
            )
            .limit(1)
        )
        if referenced is None:
            snapshots = list(
                (
                    await session.scalars(select(KnowledgeSnapshot))
                ).all()
            )
            for snapshot in snapshots:
                if str(version_id) in {
                    str(index_version_id)
                    for index_version_id in locked_index_version_ids(
                        snapshot.manifest,
                        snapshot.index_version_id,
                    )
                }:
                    referenced = snapshot.id
                    break
        if referenced is not None:
            raise ValueError(
                "knowledge version is referenced by a historical snapshot"
            )

        index_version = await session.scalar(
            select(KnowledgeIndexVersion).where(
                KnowledgeIndexVersion.source_version_id == version_id
            )
        )
        if index_version is not None:
            await self._index.delete_version(index_version, version_id)

        session.add(
            QaAdminAuditLog(
                resource_type="knowledge_version",
                resource_id=str(version.id),
                action="delete",
                actor=actor,
                details={"version": version.version},
            )
        )
        await session.delete(version)
        await session.flush()


def _citation_response(row: QaCitation) -> QaCitationResponse:
    return QaCitationResponse(
        id=row.id,
        answer_id=row.answer_id,
        chunk_id=row.chunk_id,
        citation_key=row.citation_key,
        source_title=row.source_title,
        version_label=row.version_label,
        locator=row.locator,
        excerpt=row.excerpt,
        source_uri=row.source_uri,
        checksum=row.checksum,
        created_at=row.created_at or datetime.now(UTC),
    )


def _tool_call_response(row: QaToolCall) -> QaToolCallResponse:
    return QaToolCallResponse(
        id=row.id,
        answer_id=row.answer_id,
        tool_name=row.tool_name,
        tool_status=row.tool_status,
        arguments=dict(row.arguments or {}),
        result=dict(row.result) if row.result is not None else None,
        limitations=list(row.limitations or []),
        duration_ms=row.duration_ms,
        created_at=row.created_at or datetime.now(UTC),
    )


def _map_action_response(row: QaMapAction) -> QaMapActionResponse:
    return QaMapActionResponse(
        id=row.id,
        answer_id=row.answer_id,
        action_type=row.action_type,
        payload=dict(row.payload or {}),
        valid_until=row.valid_until,
        created_at=row.created_at or datetime.now(UTC),
    )


def _session_response(row: QaSession) -> QaSessionResponse:
    return QaSessionResponse(
        id=row.id,
        created_by=row.created_by,
        event_id=row.event_id,
        snapshot_id=row.snapshot_id,
        title=row.title,
        created_at=row.created_at or datetime.now(UTC),
        updated_at=row.updated_at or datetime.now(UTC),
    )
