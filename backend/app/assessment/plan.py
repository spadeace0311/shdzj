from dataclasses import dataclass

from app.events.domain import EventKind


@dataclass(frozen=True, slots=True)
class PlannedAssessmentTask:
    task_key: str
    task_type: str
    component: str
    priority: int
    sequence: int
    deadline_offset_seconds: int
    max_attempts: int = 3


_TASKS = (
    PlannedAssessmentTask(
        task_key="intensity.model",
        task_type="intensity",
        component="model_intensity",
        priority=100,
        sequence=1,
        deadline_offset_seconds=60,
    ),
    PlannedAssessmentTask(
        task_key="intensity.instrument",
        task_type="intensity",
        component="instrument_intensity",
        priority=99,
        sequence=2,
        deadline_offset_seconds=60,
    ),
    PlannedAssessmentTask(
        task_key="intensity.fusion",
        task_type="intensity",
        component="fusion_intensity",
        priority=98,
        sequence=3,
        deadline_offset_seconds=90,
    ),
    PlannedAssessmentTask(
        task_key="loss.population",
        task_type="loss",
        component="population_impact",
        priority=90,
        sequence=4,
        deadline_offset_seconds=120,
    ),
    PlannedAssessmentTask(
        task_key="loss.casualties",
        task_type="loss",
        component="casualties",
        priority=89,
        sequence=5,
        deadline_offset_seconds=120,
    ),
    PlannedAssessmentTask(
        task_key="loss.buildings",
        task_type="loss",
        component="building_damage",
        priority=88,
        sequence=6,
        deadline_offset_seconds=150,
    ),
    PlannedAssessmentTask(
        task_key="loss.economic",
        task_type="loss",
        component="economic_loss",
        priority=87,
        sequence=7,
        deadline_offset_seconds=180,
    ),
    PlannedAssessmentTask(
        task_key="report.rapid_assessment",
        task_type="report",
        component="rapid_assessment_report",
        priority=30,
        sequence=8,
        deadline_offset_seconds=240,
    ),
    PlannedAssessmentTask(
        task_key="workgroup.response_tasks",
        task_type="coordination",
        component="workgroup_tasks",
        priority=10,
        sequence=9,
        deadline_offset_seconds=300,
    ),
)


class AssessmentPlanBuilder:
    def build(
        self,
        *,
        event_kind: EventKind,
        institutional_level: str | None,
        service_level: int | None,
    ) -> tuple[PlannedAssessmentTask, ...]:
        if event_kind not in {EventKind.FORMAL, EventKind.CORRECTION}:
            raise ValueError("assessment plans require a formal or correction event")
        if institutional_level is not None and not institutional_level.strip():
            raise ValueError("institutional_level must not be blank")
        if service_level is not None and not 1 <= service_level <= 4:
            raise ValueError("service_level must be between 1 and 4")
        return _TASKS
