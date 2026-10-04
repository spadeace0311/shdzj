import pytest

from app.assessment.plan import AssessmentPlanBuilder
from app.events.domain import EventKind


def test_plan_builder_returns_priority_ordered_assessment_tasks() -> None:
    plan = AssessmentPlanBuilder().build(
        event_kind=EventKind.FORMAL,
        institutional_level="重大响应",
        service_level=2,
    )

    assert [task.task_key for task in plan] == [
        "intensity.model",
        "intensity.instrument",
        "intensity.fusion",
        "loss.population",
        "loss.casualties",
        "loss.buildings",
        "loss.economic",
        "loss.resources",
        "loss.validate",
        "artifact.production",
        "workgroup.response_tasks",
    ]
    assert [task.priority for task in plan[:3]] == [100, 99, 98]
    assert plan[-1].priority == 10
    assert [task.sequence for task in plan] == list(range(1, 12))
    assert plan[-1].deadline_offset_seconds == 300


@pytest.mark.parametrize(
    "event_kind",
    (EventKind.FORMAL, EventKind.CORRECTION, EventKind.MANUAL, EventKind.TEST, EventKind.DRILL),
)
def test_plan_builder_accepts_all_production_event_kinds(event_kind) -> None:
    plan = AssessmentPlanBuilder().build(
        event_kind=event_kind,
        institutional_level=None,
        service_level=None,
    )
    assert "artifact.production" in {task.task_key for task in plan}
    assert "report.rapid_assessment" not in {task.task_key for task in plan}


def test_plan_builder_is_deterministic_across_levels() -> None:
    builder = AssessmentPlanBuilder()

    first = builder.build(
        event_kind=EventKind.FORMAL,
        institutional_level="一般响应",
        service_level=4,
    )
    second = builder.build(
        event_kind=EventKind.CORRECTION,
        institutional_level="特别重大响应",
        service_level=1,
    )

    assert first == second
