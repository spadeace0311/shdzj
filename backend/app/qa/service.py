from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import AuthUser
from app.config import Settings, settings
from app.db import SessionFactory
from app.knowledge.index import KnowledgeFilters
from app.knowledge.models import KnowledgeSnapshot
from app.knowledge.retrieval import RetrievalResult
from app.knowledge.snapshot import KnowledgeSnapshotService
from app.qa.access import AccessPolicy
from app.qa.deepseek import DeepSeekAdapter
from app.qa.domain import ExecutionPlan
from app.qa.evidence import EvidenceBuilder, EvidencePack
from app.qa.map_actions import MapActionBuilder, ValidatedMapAction
from app.qa.models import QaAnswer, QaCitation, QaMapAction, QaQuestion, QaSession, QaToolCall
from app.qa.planner import PlanValidationError, PlanValidator
from app.qa.tools import ToolContext, ToolRegistry, build_default_registry
from app.qa.tools.registry import ToolExecution

_CITATION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(C[0-9]+)(?![A-Za-z0-9_])")
_CITATION_TOKEN_PATTERN = re.compile(
    r"\[(C[0-9]+)\]|(?<![A-Za-z0-9_])(C[0-9]+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_PENDING_CITATION_PATTERN = re.compile(
    r"(?:\[C[0-9]*|(?<![A-Za-z0-9_])C[0-9]*)$",
    re.IGNORECASE,
)
_INCOMPLETE_BRACKET_CITATION_PATTERN = re.compile(
    r"\[C[0-9]*",
    re.IGNORECASE,
)
_INCOMPLETE_BARE_CITATION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])C[0-9]*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredQaSession:
    id: UUID
    snapshot_id: UUID
    index_version_id: UUID
    event_id: UUID | None
    revision_id: UUID | None
    assessment_run_id: UUID | None
    artifact_production_run_id: UUID | None
    created_by: str
    title: str
    manifest: dict[str, Any]


class QaStateStore(Protocol):
    async def load_session(self, session_id: UUID) -> StoredQaSession: ...

    async def start_answer(self, session_id: UUID, question: str) -> UUID: ...

    async def record_tool_calls(
        self,
        answer_id: UUID,
        executions: list[ToolExecution],
    ) -> None: ...

    async def record_citations(
        self,
        answer_id: UUID,
        citations: list[Any],
    ) -> None: ...

    async def record_map_actions(
        self,
        answer_id: UUID,
        actions: list[ValidatedMapAction],
    ) -> None: ...

    async def finish_answer(
        self,
        answer_id: UUID,
        *,
        status: str,
        text: str | None,
        structured: dict[str, Any] | None,
        citation_keys: list[str],
        degraded_reasons: list[str],
        duration_ms: int,
    ) -> None: ...

    async def get_answer(self, answer_id: UUID) -> QaAnswer: ...


class _SqlAlchemyQaStateStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot_service: KnowledgeSnapshotService,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_service = snapshot_service

    async def load_session(self, session_id: UUID) -> StoredQaSession:
        async with self._session_factory() as session:
            qa_session = await session.get(QaSession, session_id)
            if qa_session is None:
                raise LookupError("qa session not found")
            snapshot = await self._snapshot_service.get(
                session,
                qa_session.snapshot_id,
            )
            return _stored_session(qa_session, snapshot)

    async def start_answer(self, session_id: UUID, question: str) -> UUID:
        async with self._session_factory() as session:
            async with session.begin():
                qa_question = QaQuestion(
                    session_id=session_id,
                    question_text=question,
                    status="running",
                )
                session.add(qa_question)
                await session.flush()
                answer = QaAnswer(
                    question_id=qa_question.id,
                    status="running",
                    citation_keys=[],
                    degraded_reasons=[],
                )
                session.add(answer)
                await session.flush()
                return answer.id

    async def record_tool_calls(
        self,
        answer_id: UUID,
        executions: list[ToolExecution],
    ) -> None:
        if not executions:
            return
        async with self._session_factory() as session:
            async with session.begin():
                for execution in executions:
                    result = execution.result
                    session.add(
                        QaToolCall(
                            answer_id=answer_id,
                            tool_name=execution.name,
                            tool_status=str(result.status),
                            arguments=_json_safe(result.parameters),
                            result={
                                "value": _json_safe(result.value),
                                "unit": result.unit,
                                "source": result.source,
                                "version": result.version,
                                "parameters": _json_safe(result.parameters),
                                "limitations": list(result.limitations),
                            },
                            limitations=list(result.limitations),
                            duration_ms=round(execution.duration_ms),
                        )
                    )

    async def record_citations(
        self,
        answer_id: UUID,
        citations: list[Any],
    ) -> None:
        if not citations:
            return
        async with self._session_factory() as session:
            async with session.begin():
                for citation in citations:
                    payload = citation.to_persistence_dict()
                    session.add(QaCitation(answer_id=answer_id, **payload))

    async def record_map_actions(
        self,
        answer_id: UUID,
        actions: list[ValidatedMapAction],
    ) -> None:
        if not actions:
            return
        async with self._session_factory() as session:
            async with session.begin():
                for action in actions:
                    payload = dict(action.payload)
                    if action.source_tool is not None:
                        payload["source_tool"] = action.source_tool
                    session.add(
                        QaMapAction(
                            answer_id=answer_id,
                            action_type=action.action_type,
                            payload=_json_safe(payload),
                            valid_until=action.valid_until,
                        )
                    )

    async def finish_answer(
        self,
        answer_id: UUID,
        *,
        status: str,
        text: str | None,
        structured: dict[str, Any] | None,
        citation_keys: list[str],
        degraded_reasons: list[str],
        duration_ms: int,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                answer = await session.get(
                    QaAnswer,
                    answer_id,
                    with_for_update=True,
                )
                if answer is None:
                    raise LookupError("qa answer not found")
                answer.status = status
                answer.text = text
                answer.structured = _json_safe(structured)
                answer.citation_keys = list(citation_keys)
                answer.degraded_reasons = list(degraded_reasons)
                answer.duration_ms = duration_ms
                answer.completed_at = datetime.now(UTC)
                question = await session.get(QaQuestion, answer.question_id)
                if question is not None:
                    question.status = status
                qa_session = await session.scalar(
                    select(QaSession)
                    .join(QaQuestion, QaQuestion.session_id == QaSession.id)
                    .where(QaQuestion.id == answer.question_id)
                )
                if qa_session is not None:
                    qa_session.updated_at = datetime.now(UTC)

    async def get_answer(self, answer_id: UUID) -> QaAnswer:
        async with self._session_factory() as session:
            answer = await session.get(QaAnswer, answer_id)
            if answer is None:
                raise LookupError("qa answer not found")
            return answer


class QuestionOrchestrator:
    def __init__(
        self,
        *,
        deepseek: DeepSeekAdapter | Any | None = None,
        retriever: Any | None = None,
        retriever_factory: Callable[[UUID], Any] | None = None,
        registry: ToolRegistry | Any | None = None,
        plan_validator: PlanValidator | None = None,
        store: QaStateStore | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        snapshot_service: KnowledgeSnapshotService | None = None,
        access_policy: AccessPolicy | None = None,
        map_action_builder: MapActionBuilder | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        settings_override: Settings | None = None,
        streaming: bool = True,
    ) -> None:
        runtime_settings = settings_override or settings
        self._deepseek = deepseek or DeepSeekAdapter(runtime_settings)
        self._retriever = retriever
        self._retriever_factory = retriever_factory
        self._registry = registry or build_default_registry()
        self._validator = plan_validator or PlanValidator()
        self._snapshot_service = snapshot_service or KnowledgeSnapshotService()
        self._store = store or _SqlAlchemyQaStateStore(
            session_factory,
            self._snapshot_service,
        )
        self._session_factory = session_factory
        self._access = access_policy or AccessPolicy()
        self._map_actions = map_action_builder or MapActionBuilder()
        self._evidence = evidence_builder or EvidenceBuilder()
        self._streaming = streaming

    async def ask(
        self,
        question: str,
        *,
        session_id: UUID,
        user: AuthUser,
    ) -> AsyncIterator[AnswerEvent]:
        started_at = perf_counter()
        if not isinstance(question, str) or not question.strip():
            yield AnswerEvent(
                "error",
                {"code": "invalid_question", "recoverable": False},
            )
            return

        try:
            stored = await self._store.load_session(session_id)
        except LookupError:
            yield AnswerEvent(
                "error",
                {"code": "session_not_found", "recoverable": False},
            )
            return

        answer_id = await self._store.start_answer(session_id, question.strip())
        terminal_status: str | None = None
        try:
            if not self._access.can_read_event(user, stored.event_id):
                await self._finish(
                    answer_id,
                    started_at,
                    status="unavailable",
                    text="无法确认",
                    structured={"missing": ["event_access"]},
                    citation_keys=[],
                    degraded_reasons=["event_access"],
                )
                terminal_status = "unavailable"
                yield AnswerEvent(
                    "answer_completed",
                    {"answer_id": str(answer_id), "status": "unavailable"},
                )
                return

            try:
                raw_plan = await self._deepseek.plan(
                    question,
                    _planning_context(stored, user),
                    self._registry.catalog(),
                )
                plan = self._validator.validate(raw_plan, self._registry)
            except (PlanValidationError, Exception) as exc:
                del exc
                await self._finish(
                    answer_id,
                    started_at,
                    status="unavailable",
                    text="无法确认",
                    structured={"missing": ["valid_execution_plan"]},
                    citation_keys=[],
                    degraded_reasons=["plan_unavailable"],
                )
                terminal_status = "unavailable"
                yield AnswerEvent(
                    "answer_completed",
                    {"answer_id": str(answer_id), "status": "unavailable"},
                )
                return

            retrieval_result, executions = await asyncio.gather(
                self._retrieve(plan, stored),
                self._execute_tools(plan, stored, user),
            )
            await self._store.record_tool_calls(answer_id, executions)

            pack = self._evidence.build(
                retrieval_result.evidence,
                executions,
            )
            degraded_reasons = [
                *retrieval_result.degradation_reason,
            ]
            for event in _tool_events(answer_id, executions):
                yield event
            yield AnswerEvent(
                "retrieval",
                {
                    "answer_id": str(answer_id),
                    "count": len(pack.primary),
                    "degraded": retrieval_result.degraded or bool(pack.conflict_notes),
                },
            )

            await self._store.record_citations(answer_id, list(pack.citations))

            if not pack.primary and not _has_usable_tool(executions):
                await self._finish(
                    answer_id,
                    started_at,
                    status="unavailable",
                    text="无法确认",
                    structured={"missing": ["knowledge_evidence"]},
                    citation_keys=[],
                    degraded_reasons=["knowledge_evidence"],
                )
                terminal_status = "unavailable"
                yield AnswerEvent(
                    "answer_completed",
                    {"answer_id": str(answer_id), "status": "unavailable"},
                )
                return

            yield AnswerEvent(
                "answer_started",
                {"answer_id": str(answer_id)},
            )

            stream_invalid_citation_keys: list[str] = []
            try:
                if self._streaming and hasattr(self._deepseek, "stream_answer"):
                    text = ""
                    stream_citations = _CitationStreamSanitizer(
                        {
                            citation.citation_key
                            for citation in pack.citations
                            if citation.model_exported
                        }
                    )
                    draft_structured: dict[str, Any] = {}
                    draft_citation_keys: list[str] = []
                    draft_degraded: list[str] = []
                    async for chunk in self._deepseek.stream_answer(
                        question,
                        _answer_context(stored),
                        pack.model_evidence,
                        _tool_payloads(executions),
                    ):
                        if not isinstance(chunk, str) or not chunk:
                            continue
                        safe_chunk = stream_citations.feed(chunk)
                        if not safe_chunk:
                            continue
                        text += safe_chunk
                        yield AnswerEvent("answer_delta", {"text": safe_chunk})
                    trailing_text = stream_citations.finish()
                    if trailing_text:
                        text += trailing_text
                        yield AnswerEvent(
                            "answer_delta",
                            {"text": trailing_text},
                        )
                    stream_invalid_citation_keys = stream_citations.invalid_keys
                else:
                    draft = await self._deepseek.answer(
                        question,
                        _answer_context(stored),
                        pack.model_evidence,
                        _tool_payloads(executions),
                    )
                    text = draft.text
                    draft_structured = dict(draft.structured)
                    draft_citation_keys = list(draft.citation_keys)
                    draft_degraded = list(draft.degraded_reasons)
                    yield AnswerEvent("answer_delta", {"text": text})
            except asyncio.CancelledError:
                await self._finish_partial(
                    answer_id,
                    started_at,
                    text,
                    pack,
                    [],
                )
                terminal_status = "partial"
                raise
            except Exception:
                text = locals().get("text", "") or ""
                await self._finish_partial(
                    answer_id,
                    started_at,
                    text,
                    pack,
                    ["model_interrupted"],
                    status="partial",
                )
                terminal_status = "partial"
                yield AnswerEvent(
                    "error",
                    {"code": "model_interrupted", "recoverable": True},
                )
                return

            (
                text,
                citation_keys,
                invalid_citation_keys,
            ) = _validate_citations(
                text,
                pack.citations,
                draft_citation_keys,
            )
            invalid_citation_keys = _ordered_unique(
                [
                    *invalid_citation_keys,
                    *stream_invalid_citation_keys,
                ]
            )
            if invalid_citation_keys:
                degraded_reasons.append("invalid_citation_key")

            structured = dict(draft_structured)
            if pack.conflict_notes:
                structured["conflict_notes"] = list(pack.conflict_notes)
                disclosed = _append_conflict_notes(text, pack.conflict_notes)
                if disclosed != text:
                    yield AnswerEvent(
                        "answer_delta",
                        {"text": disclosed[len(text) :]},
                    )
                text = disclosed
            if pack.authority_notes:
                structured["authority_notes"] = list(pack.authority_notes)

            degraded_reasons.extend(draft_degraded)
            degraded_reasons = _ordered_unique(degraded_reasons)
            if invalid_citation_keys:
                structured["invalid_citation_keys"] = invalid_citation_keys

            actions = self._map_actions.build(plan.map_intents, executions)
            await self._store.record_map_actions(answer_id, actions)
            await self._finish(
                answer_id,
                started_at,
                status="completed",
                text=text,
                structured=structured or None,
                citation_keys=citation_keys,
                degraded_reasons=degraded_reasons,
            )
            terminal_status = "completed"

            for action in actions:
                yield AnswerEvent("map_action", action.to_dict())
            yield AnswerEvent(
                "answer_completed",
                {
                    "answer_id": str(answer_id),
                    "status": "completed",
                    "citations": [citation.to_event_dict() for citation in pack.citations],
                },
            )
        finally:
            if terminal_status is None:
                await self._safe_finish_partial(
                    answer_id,
                    started_at,
                    locals().get("text", "") or "",
                    locals().get("pack"),
                    ["orchestration_interrupted"],
                )

    async def finalize(self, answer_id: UUID | str) -> QaAnswer:
        return await self._store.get_answer(_uuid(answer_id))

    async def _retrieve(
        self,
        plan: ExecutionPlan,
        stored: StoredQaSession,
    ) -> RetrievalResult:
        if not plan.knowledge_queries:
            return RetrievalResult((), False, ())
        retriever = self._retriever
        if self._retriever_factory is not None:
            retriever = self._retriever_factory(stored.index_version_id)
            if inspect.isawaitable(retriever):
                retriever = await retriever
        if retriever is None:
            return RetrievalResult(
                (),
                True,
                ("retrieval_unavailable",),
            )

        filters = KnowledgeFilters(
            access_levels=("public", "internal", "restricted"),
            event_id=stored.event_id,
            global_only=stored.event_id is None,
        )

        async def search(query: Any) -> RetrievalResult:
            try:
                result = await retriever.search(
                    query.text,
                    filters,
                    query.top_k,
                )
            except Exception:
                return RetrievalResult(
                    (),
                    True,
                    ("retrieval_unavailable",),
                )
            if not isinstance(result, RetrievalResult):
                return RetrievalResult(
                    (),
                    True,
                    ("retrieval_unavailable",),
                )
            return result

        results = await asyncio.gather(*(search(query) for query in plan.knowledge_queries))
        evidence: dict[UUID, Any] = {}
        reasons: list[str] = []
        degraded = False
        for result in results:
            degraded = degraded or result.degraded
            reasons.extend(result.degradation_reason)
            for item in result.evidence:
                evidence.setdefault(item.chunk_id, item)
        return RetrievalResult(
            tuple(evidence.values()),
            degraded,
            tuple(_ordered_unique(reasons)),
        )

    async def _execute_tools(
        self,
        plan: ExecutionPlan,
        stored: StoredQaSession,
        user: AuthUser,
    ) -> list[ToolExecution]:
        if not plan.tool_calls:
            return []
        async with self._session_factory() as caller_session:
            context = ToolContext(
                session=caller_session,
                user=user,
                event_id=stored.event_id,
                revision_id=stored.revision_id,
                assessment_run_id=stored.assessment_run_id,
                snapshot_id=stored.snapshot_id,
                index_version_id=stored.index_version_id,
                artifact_production_run_id=stored.artifact_production_run_id,
            )
            return await self._registry.execute_plan(plan.tool_calls, context)

    async def _finish_partial(
        self,
        answer_id: UUID,
        started_at: float,
        text: str,
        pack: EvidencePack | None,
        degraded_reasons: list[str],
        *,
        status: str = "partial",
    ) -> None:
        cleaned_text = text
        citation_keys: list[str] = []
        if text:
            cleaned_text, citation_keys, _ = _validate_citations(
                text,
                pack.citations if pack is not None else (),
                [],
            )
        structured: dict[str, Any] = {}
        if pack is not None and pack.conflict_notes:
            structured["conflict_notes"] = list(pack.conflict_notes)
        if pack is not None and pack.authority_notes:
            structured["authority_notes"] = list(pack.authority_notes)
        await self._finish(
            answer_id,
            started_at,
            status=status,
            text=cleaned_text or None,
            structured=structured or None,
            citation_keys=citation_keys,
            degraded_reasons=_ordered_unique(degraded_reasons),
        )

    async def _safe_finish_partial(
        self,
        answer_id: UUID,
        started_at: float,
        text: str,
        pack: EvidencePack | None,
        degraded_reasons: list[str],
    ) -> None:
        try:
            await self._finish_partial(
                answer_id,
                started_at,
                text,
                pack,
                degraded_reasons,
                status="partial",
            )
        except Exception:
            pass

    async def _finish(
        self,
        answer_id: UUID,
        started_at: float,
        *,
        status: str,
        text: str | None,
        structured: dict[str, Any] | None,
        citation_keys: list[str],
        degraded_reasons: list[str],
    ) -> None:
        await self._store.finish_answer(
            answer_id,
            status=status,
            text=text,
            structured=structured,
            citation_keys=citation_keys,
            degraded_reasons=degraded_reasons,
            duration_ms=max(0, round((perf_counter() - started_at) * 1000)),
        )


def _stored_session(
    qa_session: QaSession,
    snapshot: KnowledgeSnapshot,
) -> StoredQaSession:
    return StoredQaSession(
        id=qa_session.id,
        snapshot_id=qa_session.snapshot_id,
        index_version_id=snapshot.index_version_id,
        event_id=snapshot.event_id,
        revision_id=snapshot.revision_id,
        assessment_run_id=snapshot.assessment_run_id,
        artifact_production_run_id=snapshot.artifact_production_run_id,
        created_by=qa_session.created_by,
        title=qa_session.title,
        manifest=dict(snapshot.manifest or {}),
    )


def _planning_context(
    stored: StoredQaSession,
    user: AuthUser,
) -> dict[str, Any]:
    return {
        "user": {
            "username": user.username,
            "role": user.role,
            "workgroup": user.workgroup,
        },
        "session": {
            "id": str(stored.id),
            "title": stored.title,
        },
        "snapshot": {
            "id": str(stored.snapshot_id),
            "event_id": _str_or_none(stored.event_id),
            "revision_id": _str_or_none(stored.revision_id),
            "assessment_run_id": _str_or_none(stored.assessment_run_id),
            "artifact_production_run_id": _str_or_none(stored.artifact_production_run_id),
            "index_version_id": str(stored.index_version_id),
            "manifest": stored.manifest,
        },
    }


def _answer_context(stored: StoredQaSession) -> dict[str, Any]:
    return {
        "session_id": str(stored.id),
        "snapshot_id": str(stored.snapshot_id),
        "event_id": _str_or_none(stored.event_id),
        "revision_id": _str_or_none(stored.revision_id),
        "assessment_run_id": _str_or_none(stored.assessment_run_id),
        "artifact_production_run_id": _str_or_none(stored.artifact_production_run_id),
        "index_version_id": str(stored.index_version_id),
    }


def _tool_events(
    answer_id: UUID,
    executions: Iterable[ToolExecution],
) -> list[AnswerEvent]:
    return [
        AnswerEvent(
            "tool",
            {
                "answer_id": str(answer_id),
                "name": execution.name,
                "status": str(execution.result.status),
                "duration_ms": round(execution.duration_ms),
            },
        )
        for execution in executions
    ]


def _tool_payloads(executions: Iterable[ToolExecution]) -> list[dict[str, Any]]:
    return [
        {
            "name": execution.name,
            "status": str(execution.result.status),
            "value": _json_safe(execution.result.value),
            "unit": execution.result.unit,
            "source": execution.result.source,
            "version": execution.result.version,
            "parameters": _json_safe(execution.result.parameters),
            "limitations": list(execution.result.limitations),
        }
        for execution in executions
    ]


def _has_usable_tool(executions: Iterable[ToolExecution]) -> bool:
    return any(str(execution.result.status) == "ok" for execution in executions)


def _validate_citations(
    text: str,
    citations: Iterable[Any],
    requested_keys: Iterable[str],
) -> tuple[str, list[str], list[str]]:
    available = {
        citation.citation_key
        for citation in citations
        if citation.model_exported
    }
    requested = [
        *requested_keys,
        *(match.group(1) for match in _CITATION_PATTERN.finditer(text)),
    ]
    ordered = _ordered_unique(key.upper() for key in requested)
    invalid = [key for key in ordered if key not in available]
    valid = [key for key in ordered if key in available]

    cleaned = text
    for key in invalid:
        cleaned = re.sub(
            rf"\[\s*{re.escape(key)}\s*\]",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +\n", "\n", cleaned).strip()
    return cleaned, valid, invalid


class _CitationStreamSanitizer:
    def __init__(self, available_keys: Iterable[str]) -> None:
        self._available = {key.upper() for key in available_keys}
        self._buffer = ""
        self.invalid_keys: list[str] = []

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        return self._drain(final=False)

    def finish(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        if not self._buffer:
            return ""
        if final:
            candidate = self._buffer
            self._buffer = ""
        else:
            pending = _PENDING_CITATION_PATTERN.search(self._buffer)
            if pending is None:
                candidate = self._buffer
                self._buffer = ""
            else:
                candidate = self._buffer[: pending.start()]
                self._buffer = self._buffer[pending.start() :]

        sanitized = _CITATION_TOKEN_PATTERN.sub(
            self._replace_citation,
            candidate,
        )
        if final:
            sanitized = _INCOMPLETE_BRACKET_CITATION_PATTERN.sub(
                "",
                sanitized,
            )
            sanitized = _INCOMPLETE_BARE_CITATION_PATTERN.sub(
                "",
                sanitized,
            )
        return sanitized

    def _replace_citation(self, match: re.Match[str]) -> str:
        key = (match.group(1) or match.group(2)).upper()
        if key not in self._available:
            self.invalid_keys.append(key)
            return ""
        return f"[{key}]"


def _append_conflict_notes(text: str, notes: Iterable[str]) -> str:
    note_list = list(notes)
    if not note_list:
        return text
    return f"{text.rstrip()}\n\n数据冲突：" + "；".join(note_list)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _str_or_none(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "AnswerEvent",
    "QaStateStore",
    "QuestionOrchestrator",
    "StoredQaSession",
]
