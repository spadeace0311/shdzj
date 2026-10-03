from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ProjectionTaskCounts(BaseModel):
    total: int = 0
    pending: int = 0
    in_progress: int = 0
    pending_review: int = 0
    completed: int = 0
    not_required: int = 0
    failed: int = 0
    overdue: int = 0
    at_risk: int = 0
    dual_version_count: int = 0


class ActiveEventResponse(BaseModel):
    event_id: UUID | None


class EventOverview(BaseModel):
    event_id: UUID
    event: dict[str, Any]
    group_count: int
    groups: list[dict[str, Any]] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    task_counts: ProjectionTaskCounts
    artifact_summary: dict[str, Any] = Field(default_factory=dict)
    alert_summary: dict[str, Any] = Field(default_factory=dict)
    dual_version_count: int = 0
    projection_version: int
    sync_status: str = "current"
    projection_lag_seconds: float = 0
    projection_source_updated_at: datetime | None = None
    updated_at: datetime


class GroupDetail(BaseModel):
    event_id: UUID
    workgroup_code: str
    group: dict[str, Any]
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    alert_summary: dict[str, Any] = Field(default_factory=dict)
    task_counts: ProjectionTaskCounts
    projection_version: int
    updated_at: datetime


class TaskDetail(BaseModel):
    id: UUID
    event_id: UUID
    task: dict[str, Any]
    contributors: list[dict[str, Any]] = Field(default_factory=list)
    deliverables: list[dict[str, Any]] = Field(default_factory=list)
    task_events: list[dict[str, Any]] = Field(default_factory=list)
    notifications: list[dict[str, Any]] = Field(default_factory=list)
    projection_version: int | None = None


class ProjectionUpdatedPayload(BaseModel):
    event_id: UUID
    projection_version: int
