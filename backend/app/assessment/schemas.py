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
