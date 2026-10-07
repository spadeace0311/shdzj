from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.knowledge.domain import (
    KnowledgeJobStatus,
    KnowledgeVersionStatus,
    SourceLayer,
)


class KnowledgeSourceCreate(BaseModel):
    source_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=256)
    layer: SourceLayer
    source_type: str = Field(min_length=1, max_length=64)
    access_level: Literal["public", "internal", "restricted"] = "internal"
    origin: str | None = Field(default=None, max_length=2048)
    allow_online_refresh: bool = False


class KnowledgeVersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=128)
    source_uri: str | None = Field(default=None, max_length=2048)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSourceResponse(BaseModel):
    id: UUID
    source_key: str
    title: str
    layer: SourceLayer
    source_type: str
    access_level: Literal["public", "internal", "restricted"]
    origin: str | None
    allow_online_refresh: bool
    is_active: bool
    created_by: str
    created_at: datetime


class KnowledgeVersionResponse(BaseModel):
    id: UUID
    source_id: UUID
    version: str
    status: KnowledgeVersionStatus
    checksum: str | None
    size_bytes: int | None
    failure_reason: str | None
    created_at: datetime
    published_at: datetime | None


class KnowledgeJobResponse(BaseModel):
    id: UUID
    version_id: UUID
    job_type: str
    status: KnowledgeJobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    request_payload: dict[str, Any]
    result_payload: dict[str, Any]
    last_error: str | None
    created_at: datetime
    completed_at: datetime | None


class KnowledgeLifecycleRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
