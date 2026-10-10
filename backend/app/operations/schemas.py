from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ManualTestTriggerRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class DrillTriggerRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class OperationsSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    daily_test_enabled: bool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    daily_window_start: time | None = None
    daily_window_end: time | None = None
    test_magnitude_min: Decimal | None = Field(default=None, ge=-2, le=12)
    test_magnitude_max: Decimal | None = Field(default=None, ge=-2, le=12)
    test_depth_min_km: Decimal | None = Field(default=None, ge=0, le=1000)
    test_depth_max_km: Decimal | None = Field(default=None, ge=0, le=1000)
    drill_conflict_policy: Literal["pause", "terminate"] | None = None


class OperationsSettingsRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_version: int
    daily_test_enabled: bool
    timezone: str
    daily_window_start: time
    daily_window_end: time
    test_magnitude_min: Decimal
    test_magnitude_max: Decimal
    test_depth_min_km: Decimal
    test_depth_max_km: Decimal
    drill_conflict_policy: str
    updated_by: str | None
    updated_at: datetime


class OperationsScheduleRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    local_date: date
    window_start: datetime
    window_end: datetime
    scheduled_at: datetime
    state: str
    scenario: dict | None
    random_seed: int
    event_id: UUID | None
    failure_reason: str | None
    triggered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OperationsRunRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    schedule_id: UUID | None
    trigger_mode: str
    operation_state: str
    priority: int
    idempotency_key: str
    triggered_by: str
    resume_required: bool
    detail: dict
    started_at: datetime | None
    paused_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OperationsTriggerResult(BaseModel):
    event_id: UUID | None
    run_id: UUID | None
    revision_id: str | None = None
    trigger_mode: str
    operation_state: str
    outcome: str
    idempotency_key: str
    event_kind: str | None = None
    triggered_assessment: bool = False


class OperationsOverviewRecord(BaseModel):
    observed_at: datetime
    real_event_priority_active: bool
    settings: OperationsSettingsRecord
    next_schedule: OperationsScheduleRecord | None
    today_schedule: OperationsScheduleRecord | None
    recent_runs: list[OperationsRunRecord]
    active_run_count: int
