from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_QUESTION_LENGTH = 2000
MAX_QA_ACTOR_LENGTH = 64


class QaAnswerStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ToolCallPlan:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    text: str
    top_k: int


@dataclass(frozen=True, slots=True)
class MapIntent:
    action_type: str
    target_ref: str | None
    reason: str
    bounds: list[float] | None = None
    radius_km: float | None = None
    layer_id: str | None = None
    layers: list[str] | dict[str, bool] | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    intent: str
    tool_calls: list[ToolCallPlan]
    knowledge_queries: list[KnowledgeQuery]
    map_intents: list[MapIntent]
    clarification: str | None


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    text: str
    structured: dict[str, Any]
    citation_keys: list[str]
    degraded_reasons: list[str]
