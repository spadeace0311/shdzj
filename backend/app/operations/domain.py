from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class TriggerMode(StrEnum):
    SCHEDULED_TEST = "scheduled_test"
    MANUAL_TEST = "manual_test"
    DRILL = "drill"
    MANUAL_EVENT = "manual_event"


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class ScheduleState(StrEnum):
    SCHEDULED = "scheduled"
    DUE = "due"
    DEFERRED = "deferred"
    TRIGGERED = "triggered"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperationsScenario:
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    depth_km: Decimal
    magnitude: Decimal
    place: str
    source_event_id: str
    random_seed: int
    boundary_version: str | None = None
