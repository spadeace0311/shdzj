from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.assessment.domain import AssessmentTaskStatus
from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.assessment.schemas import (
    AssessmentRunStatusResponse,
    AssessmentTaskStatusResponse,
    IntensityGridArtifactResponse,
    IntensityGridBandResponse,
    IntensityResultResponse,
)
from app.auth.router import require_role
from app.db import SessionFactory
from app.events.models import EarthquakeEvent
from app.intensity.domain import ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct
from app.intensity.repository import IntensityRepository
from app.loss.router import build_assessment_loss
from app.raster_tiles import MAX_TILE_ZOOM, MIN_TILE_ZOOM, render_raster_tile

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
    "/runs/{run_id}/intensity/artifact",
    response_model=IntensityGridArtifactResponse,
)
async def get_intensity_artifact(
    run_id: UUID,
    product_id: UUID,
    band: str | None = Query(default=None),
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> IntensityGridArtifactResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                product = await _fusion_product_for_run(
                    session,
                    run_id,
                    product_id,
                )
                bands, metadata = await IntensityRepository().load_raster(
                    session,
                    product.id,
                )
                selected_band, _ = _select_intensity_band(
                    metadata["bands"],
                    band,
                )
                return _intensity_artifact_response(
                    run_id,
                    product,
                    metadata,
                    selected_band,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


@router.get(
    "/runs/{run_id}/intensity/artifact/{product_id}/{band}/{z}/{x}/{y}.png",
    response_class=Response,
)
async def get_intensity_tile(
    run_id: UUID,
    product_id: UUID,
    band: str,
    z: int,
    x: int,
    y: int,
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> Response:
    if not MIN_TILE_ZOOM <= z <= MAX_TILE_ZOOM:
        raise HTTPException(
            status_code=422,
            detail="tile zoom must be between 0 and 22",
        )
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                product = await _fusion_product_for_run(
                    session,
                    run_id,
                    product_id,
                )
                values, metadata = await IntensityRepository().load_raster(
                    session,
                    product.id,
                )
                _, band_index = _select_intensity_band(
                    metadata["bands"],
                    band,
                )
                png = render_raster_tile(
                    values[band_index],
                    metadata,
                    z,
                    x,
                    y,
                )
                return Response(content=png, media_type="image/png")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


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


async def _fusion_product_for_run(
    session,
    run_id: UUID,
    product_id: UUID,
) -> IntensityFieldProduct:
    product = await session.scalar(
        select(IntensityFieldProduct).where(
            IntensityFieldProduct.id == product_id,
            IntensityFieldProduct.run_id == run_id,
        )
    )
    if product is None:
        raise LookupError("intensity_fusion_product_not_found")
    if product.product_type != ProductType.FUSION.value:
        raise LookupError("intensity_fusion_product_not_found")
    if product.status not in {
        ProductStatus.AVAILABLE.value,
        ProductStatus.PARTIAL.value,
    }:
        raise LookupError("intensity_artifact_unavailable")
    return product


def _select_intensity_band(
    manifest_bands: list[dict],
    requested_band: str | None,
) -> tuple[str, int]:
    entries = [dict(item) for item in manifest_bands if isinstance(item, dict)]
    names = [str(item["name"]) for item in entries if "name" in item]
    if not names:
        raise LookupError("intensity_raster_band_not_found")
    selected = requested_band
    if selected is None:
        selected = "value" if "value" in names else names[0]
    if selected not in names:
        raise LookupError("intensity_raster_band_not_found")
    return selected, names.index(selected)


def _intensity_artifact_response(
    run_id: UUID,
    product: IntensityFieldProduct,
    metadata: dict,
    selected_band: str,
) -> IntensityGridArtifactResponse:
    manifest_bands = metadata["bands"]
    entries = [dict(item) for item in manifest_bands if isinstance(item, dict)]
    origin_x = float(metadata["origin_x"])
    origin_y = float(metadata["origin_y"])
    resolution_m = float(metadata["resolution_m"])
    width = int(metadata["width"])
    height = int(metadata["height"])
    bbox = (
        origin_x,
        origin_y - height * resolution_m,
        origin_x + width * resolution_m,
        origin_y,
    )
    tile_template = (
        f"/api/v1/assessments/runs/{run_id}/intensity/artifact/"
        f"{product.id}/{selected_band}/{{z}}/{{x}}/{{y}}.png"
    )
    return IntensityGridArtifactResponse(
        product_id=str(product.id),
        checksum=str(metadata["checksum"]),
        width=width,
        height=height,
        srid=int(metadata["srid"]),
        bbox=bbox,
        coverage_ratio=float(product.coverage_ratio),
        bands=[
            IntensityGridBandResponse(
                name=str(item["name"]),
                unit=item.get("unit"),
                precision=item.get("precision"),
            )
            for item in entries
            if "name" in item
        ],
        tile_template=tile_template,
    )
