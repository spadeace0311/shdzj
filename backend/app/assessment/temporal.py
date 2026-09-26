from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowInput:
    event_id: str
    revision_id: str
    outbox_id: str


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowResult:
    run_id: str
    task_count: int


@workflow.defn
class AssessmentWorkflow:
    @workflow.run
    async def run(
        self,
        request: AssessmentWorkflowInput,
    ) -> AssessmentWorkflowResult:
        return await workflow.execute_activity(
            "prepare_assessment",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
            ),
        )


class AssessmentActivities:
    def __init__(self, session_factory: object) -> None:
        self._session_factory = session_factory

    @activity.defn(name="prepare_assessment")
    async def prepare_assessment(
        self,
        request: AssessmentWorkflowInput,
    ) -> AssessmentWorkflowResult:
        from app.assessment.repository import AssessmentRepository

        repository = AssessmentRepository()
        async with self._session_factory() as session:
            async with session.begin():
                run = await repository.ensure_run_from_outbox(
                    session,
                    event_id=request.event_id,
                    revision_id=request.revision_id,
                    outbox_id=request.outbox_id,
                )
                task_count = await repository.count_tasks(session, run.id)
        return AssessmentWorkflowResult(run_id=str(run.id), task_count=task_count)
