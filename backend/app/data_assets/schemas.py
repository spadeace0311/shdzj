from datetime import datetime

from pydantic import BaseModel, Field

from app.data_assets.domain import AssetVersionStatus, ImportJobStatus


class AssetSummaryResponse(BaseModel):
    asset_key: str
    region_id: str
    name: str
    data_type: str
    spatial_granularity: str
    responsibility_unit: str
    update_interval_days: int
    is_core: bool
    published_version: str | None
    published_at: datetime | None
    update_due_at: datetime | None
    is_update_overdue: bool


class ImportAcceptedResponse(BaseModel):
    job_id: str
    asset_key: str
    version: str
    status: ImportJobStatus
    version_id: str


class ValidationIssueResponse(BaseModel):
    severity: str
    code: str
    message: str
    row_number: int | None
    field_name: str | None


class ValidationReportResponse(BaseModel):
    version_id: str
    status: AssetVersionStatus
    errors: list[ValidationIssueResponse]
    warnings: list[ValidationIssueResponse]
    statistics: dict
    checked_at: datetime


class AssetVersionResponse(BaseModel):
    id: str
    asset_key: str
    region_id: str
    version: str
    status: AssetVersionStatus
    source_uri: str
    source_crs: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    quality_grade: str | None
    change_note: str | None
    schema_summary: dict
    record_count: int
    checksum: str
    imported_by: str
    reviewed_by: str | None
    imported_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    retired_at: datetime | None
    validation_errors: list[ValidationIssueResponse]
    validation_warnings: list[ValidationIssueResponse]
    statistics: dict


class ImportJobResponse(BaseModel):
    job_id: str
    asset_key: str
    version: str
    version_id: str
    status: ImportJobStatus
    error_summary: str | None
    validation_errors: list[ValidationIssueResponse]
    validation_warnings: list[ValidationIssueResponse]
    statistics: dict
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class LifecycleActionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
