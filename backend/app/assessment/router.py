from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.assessment.domain import AssessmentTaskStatus
from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.assessment.schemas import (
    AssessmentRunStatusResponse,
    AssessmentTaskStatusResponse,
)
from app.auth.router import require_role
from app.db import SessionFactory

router = APIRouter(prefix="/api/v1/assessments", tags=["assessments"])

_ASSESSMENT_READ_ROLES = (
    "superadmin",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)


def get_assessment_repository() -> AssessmentRepository:
    return AssessmentRepository()


@router.get(
    "/events/{event_id}/current",
    response_model=AssessmentRunStatusResponse,
)
async def get_current_assessment(
    event_id: UUID,
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> AssessmentRunStatusResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_current_run(
                    session,
                    event_id=str(event_id),
                )
                if run is None:
                    raise LookupError("assessment_run_not_found")
                tasks = await repository.list_tasks(session, run.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="assessment storage is unavailable") from exc
    return _run_response(run, tasks)


def _run_response(
    run: AssessmentRun,
    tasks: list[AssessmentTask],
) -> AssessmentRunStatusResponse:
    return AssessmentRunStatusResponse(
        run_id=str(run.id),
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        run_no=run.run_no,
        status=run.status,
        t1_at=run.t1_at,
        deadline_at=run.deadline_at,
        completed_task_count=sum(
            task.status == AssessmentTaskStatus.SUCCEEDED for task in tasks
        ),
        failed_task_count=sum(
            task.status == AssessmentTaskStatus.FAILED for task in tasks
        ),
        total_task_count=len(tasks),
        tasks=[
            AssessmentTaskStatusResponse(
                id=str(task.id),
                task_key=task.task_key,
                task_type=task.task_type,
                component=task.component,
                priority=task.priority,
                sequence=task.sequence,
                status=task.status,
                deadline_at=task.deadline_at,
                started_at=task.started_at,
                completed_at=task.completed_at,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
                last_error=task.last_error,
            )
            for task in tasks
        ],
    )
