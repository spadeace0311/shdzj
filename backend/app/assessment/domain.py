from enum import StrEnum


class AssessmentRevisionSupersededError(ValueError):
    def __init__(self, revision_id: object) -> None:
        super().__init__("assessment revision is superseded")
        self.revision_id = revision_id


class AssessmentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AssessmentTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"
