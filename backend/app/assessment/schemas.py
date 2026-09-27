from datetime import datetime

from pydantic import BaseModel

from app.assessment.domain import AssessmentRunStatus, AssessmentTaskStatus


class AssessmentTaskStatusResponse(BaseModel):
    id: str
    task_key: str
    task_type: str
    component: str
    priority: int
    sequence: int
    status: AssessmentTaskStatus
    deadline_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempt_count: int
    max_attempts: int
    last_error: str | None


class IntensityProductSummary(BaseModel):
    product_id: str
    product_type: str
    status: str
    algorithm_version: str
    parameter_version: str
    strategy_version: str | None
    grid_definition_version: str
    region_profile_version: str
    quality_grade: str | None
    coverage_ratio: float
    output_checksum: str | None
    source_product_id: str | None
    observed_at: datetime | None
    completed_at: datetime | None
    published_at: datetime | None
    statistics: dict


class IntensityResultResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    run_status: str
    deadline_basis_at: datetime
    deadline_at: datetime
    deadline_exceeded_at: datetime | None
    completed_at: datetime | None
    superseded_by_run_id: str | None
    effective_run_id: str | None
    effective_revision_id: str | None
    is_latest_revision: bool
    is_fallback: bool
    products: list[IntensityProductSummary]


class AssessmentRunStatusResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    run_no: int
    status: AssessmentRunStatus
    t1_at: datetime
    deadline_at: datetime
    completed_task_count: int
    failed_task_count: int
    total_task_count: int
    tasks: list[AssessmentTaskStatusResponse]
    intensity: IntensityResultResponse | None = None
