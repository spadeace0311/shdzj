from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class QaSessionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=256)
    event_id: UUID | None = None


class QaSessionResponse(BaseModel):
    id: UUID
    created_by: str
    event_id: UUID | None
    snapshot_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class QaQuestionCreate(BaseModel):
    question: str = Field(min_length=1)


class QaCitationResponse(BaseModel):
    id: UUID
    answer_id: UUID
    chunk_id: UUID | None
    citation_key: str
    source_title: str
    version_label: str
    locator: str | None
    excerpt: str
    source_uri: str | None
    checksum: str
    created_at: datetime


class QaToolCallResponse(BaseModel):
    id: UUID
    answer_id: UUID
    tool_name: str
    tool_status: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    limitations: list[Any]
    duration_ms: int | None
    created_at: datetime


class QaMapActionResponse(BaseModel):
    id: UUID
    answer_id: UUID
    action_type: str
    payload: dict[str, Any]
    valid_until: datetime | None
    created_at: datetime


class QaFeedbackCreate(BaseModel):
    helpful: bool | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class QaFeedbackResponse(BaseModel):
    id: UUID
    answer_id: UUID
    created_by: str
    helpful: bool | None
    rating: int | None
    comment: str | None
    created_at: datetime


class QaAnswerView(BaseModel):
    id: UUID
    question_id: UUID
    session_id: UUID
    status: str
    text: str | None
    structured: dict[str, Any] | None
    citation_keys: list[str]
    degraded_reasons: list[str]
    duration_ms: int | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    citations: list[QaCitationResponse]
    tool_calls: list[QaToolCallResponse]
    map_actions: list[QaMapActionResponse]


class OperationLogPurgeFilters(BaseModel):
    event_id: UUID | None = None
    answer_id: UUID | None = None
    actor: str | None = None
    before: datetime | None = None


class OperationLogPurgeResponse(BaseModel):
    deleted: int
