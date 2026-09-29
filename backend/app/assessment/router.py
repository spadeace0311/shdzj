from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.assessment.domain import AssessmentTaskStatus
from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.assessment.schemas import (
    AssessmentRunStatusResponse,
    AssessmentTaskStatusResponse,
    IntensityResultResponse,
)
from app.auth.router import require_role
from app.db import SessionFactory
from app.events.models import EarthquakeEvent
from app.intensity.repository import IntensityRepository
from app.loss.router import build_assessment_loss

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
    "/runs/{run_id}/intensity",
    response_model=IntensityResultResponse,
)
async def get_intensity_result(
    run_id: UUID,
    assessment_repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
):
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await assessment_repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                event = await session.get(EarthquakeEvent, run.event_id)
                if event is None:
                    raise LookupError("assessment_event_not_found")
                effective = await assessment_repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                products = await IntensityRepository().list_products(
                    session,
                    run.id,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="assessment storage is unavailable") from exc
    return IntensityResultResponse(
        run_id=str(run.id),
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        run_status=run.status,
        deadline_basis_at=run.deadline_basis_at,
        deadline_at=run.deadline_at,
        deadline_exceeded_at=run.deadline_exceeded_at,
        completed_at=run.completed_at,
        superseded_by_run_id=(
            str(run.superseded_by_run_id)
            if run.superseded_by_run_id is not None
            else None
        ),
        effective_run_id=str(effective.id) if effective is not None else None,
        effective_revision_id=(
            str(effective.revision_id) if effective is not None else None
        ),
        is_latest_revision=event.latest_assessment_run_id == run.id,
        is_fallback=effective is not None and effective.id != run.id,
        products=products,
        data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
        data_asset_snapshot=run.data_asset_snapshot_result,
    )


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
                event = await session.get(EarthquakeEvent, run.event_id)
                effective = await repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                result_run = effective or run
                products = await IntensityRepository().list_products(
                    session,
                    result_run.id,
                )
                intensity = IntensityResultResponse(
                    run_id=str(result_run.id),
                    event_id=str(result_run.event_id),
                    revision_id=str(result_run.revision_id),
                    run_status=result_run.status,
                    deadline_basis_at=result_run.deadline_basis_at,
                    deadline_at=result_run.deadline_at,
                    deadline_exceeded_at=result_run.deadline_exceeded_at,
                    completed_at=result_run.completed_at,
                    superseded_by_run_id=(
                        str(result_run.superseded_by_run_id)
                        if result_run.superseded_by_run_id is not None
                        else None
                    ),
                    effective_run_id=str(effective.id) if effective is not None else None,
                    effective_revision_id=(
                        str(effective.revision_id) if effective is not None else None
                    ),
                    is_latest_revision=event.latest_assessment_run_id == result_run.id,
                    is_fallback=effective is not None and effective.id != run.id,
                    products=products,
                    data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
                    data_asset_snapshot=run.data_asset_snapshot_result,
                )
                loss = await build_assessment_loss(
                    session,
                    run,
                    effective,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="assessment storage is unavailable") from exc
    return _run_response(run, tasks, intensity=intensity, loss=loss)


def _run_response(
    run: AssessmentRun,
    tasks: list[AssessmentTask],
    *,
    intensity: IntensityResultResponse | None,
    loss: object | None = None,
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
        intensity=intensity,
        loss=loss,
        data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
        data_asset_snapshot=run.data_asset_snapshot_result,
    )
