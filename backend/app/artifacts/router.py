from __future__ import annotations

import mimetypes
import uuid
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from app.artifacts.catalog import load_catalog
from app.artifacts.models import (
    ArtifactPublication,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.permissions import can_rebuild_artifacts
from app.artifacts.schemas import (
    ArtifactRebuildRequest,
    ArtifactSummaryResponse,
    ProductionRunResponse,
)
from app.artifacts.service import (
    ArtifactOverrideService,
    ArtifactOverrideValidationError,
    ArtifactProductionService,
    IdempotencyConflictError,
)
from app.artifacts.storage import ArtifactStore
from app.artifacts.workflow import (
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
)
from app.auth.router import get_current_user, require_role
from app.auth.service import AuthUser
from app.config import settings
from app.db import SessionFactory
from app.events.models import EarthquakeEvent

router = APIRouter(prefix="/api/v1", tags=["artifacts"])

_ARTIFACT_READ_ROLES = (
    "superadmin",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)


def get_artifact_production_service() -> ArtifactProductionService:
    return ArtifactProductionService(session_factory=SessionFactory)


def get_artifact_override_service() -> ArtifactOverrideService:
    return ArtifactOverrideService(session_factory=SessionFactory)


class ArtifactWorkflowStarter:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    async def start(self, request: ArtifactProductionWorkflowInput) -> None:
        client = self._client or await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
        )
        try:
            await client.start_workflow(
                ArtifactProductionWorkflow.run,
                request,
                id=f"artifact-production:{request.production_run_id}",
                task_queue=settings.temporal_task_queue,
                execution_timeout=timedelta(
                    seconds=settings.assessment_workflow_safety_timeout_seconds
                ),
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        finally:
            if self._client is None:
                await client.close()


def get_artifact_workflow_starter() -> ArtifactWorkflowStarter:
    return ArtifactWorkflowStarter()


@lru_cache
def _catalog():
    return load_catalog(settings.artifact_catalog_path)


def _display_name(artifact_key: str) -> str:
    for definition in _catalog().definitions:
        if definition.artifact_key == artifact_key:
            return definition.display_name
    return artifact_key


def _mode_marker(production_mode: str) -> str | None:
    return {
        "live": None,
        "manual": None,
        "test": "【测试】",
        "drill": "【演练】",
        "replay": "【测试回放】",
    }.get(production_mode)


def _media_type(format_name: str) -> str:
    return mimetypes.guess_type(f"file.{format_name}")[0] or "application/octet-stream"


def _has_thumbnail(format_name: str) -> bool:
    return format_name.lower() in {"jpg", "jpeg", "png", "webp"}


def _artifact_summary(artifact: GeneratedArtifact) -> ArtifactSummaryResponse:
    return ArtifactSummaryResponse(
        artifact_id=artifact.id,
        artifact_key=artifact.artifact_key,
        output_profile=artifact.output_profile,
        display_name=_display_name(artifact.artifact_key),
        artifact_version=artifact.artifact_version,
        status=artifact.status,
        quality_grade=artifact.quality_grade or "",
        needs_review=artifact.needs_review,
        production_mode=artifact.production_mode,
        publication_mode=artifact.publication_mode,
        file_name=artifact.file_name,
        format=artifact.format,
        size_bytes=artifact.size_bytes,
        generated_at=artifact.generated_at,
        download_url=f"/api/v1/artifacts/{artifact.id}/download",
        thumbnail_url=(
            f"/api/v1/artifacts/{artifact.id}/thumbnail"
            if _has_thumbnail(artifact.format)
            else None
        ),
    )


async def _artifacts_for_run(session, run_id: UUID) -> list[GeneratedArtifact]:
    rows = await session.scalars(
        select(GeneratedArtifact)
        .where(
            GeneratedArtifact.production_run_id == run_id,
            GeneratedArtifact.is_final.is_(True),
        )
        .order_by(
            GeneratedArtifact.artifact_key,
            GeneratedArtifact.output_profile,
        )
    )
    return list(rows)


def _version_maps(snapshot: ProductionInputSnapshot | None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    manifest = dict(snapshot.manifest or {}) if snapshot is not None else {}
    template_versions: dict[str, str] = {}
    data_asset_versions: dict[str, str] = {}
    for item in manifest.get("templates", ()):
        if not isinstance(item, dict):
            continue
        key = item.get("template_key") or item.get("key")
        value = item.get("version") or item.get("checksum")
        if isinstance(key, str):
            template_versions[key] = str(value) if value is not None else ""
    for item in manifest.get("assets", ()):
        if not isinstance(item, dict):
            continue
        key = item.get("asset_key")
        value = item.get("version") or item.get("checksum")
        if isinstance(key, str):
            data_asset_versions[key] = str(value) if value is not None else ""
    renderer_versions = {
        str(key): str(value)
        for key, value in (manifest.get("renderer_versions") or {}).items()
    }
    return template_versions, data_asset_versions, renderer_versions


async def _production_run_response(
    session,
    run: ProductionRun,
) -> ProductionRunResponse:
    tasks = list(
        (
            await session.scalars(
                select(ProductionTask)
                .where(ProductionTask.production_run_id == run.id)
                .order_by(ProductionTask.sequence, ProductionTask.id)
            )
        ).all()
    )
    artifacts = await _artifacts_for_run(session, run.id)
    snapshot = await session.scalar(
        select(ProductionInputSnapshot).where(
            ProductionInputSnapshot.production_run_id == run.id
        )
    )
    template_versions, data_asset_versions, renderer_versions = _version_maps(
        snapshot
    )
    return ProductionRunResponse(
        production_run_id=run.id,
        assessment_run_id=run.assessment_run_id,
        status=run.status,
        production_mode=run.production_mode,
        launch_mode=run.launch_mode,
        generation_seq=run.generation_seq,
        generation_scope=run.generation_scope,
        deadline_basis_at=run.deadline_basis_at,
        deadline_at=run.deadline_at,
        required_output_count=len(run.required_outputs or ()),
        complete_count=sum(task.status == "succeeded" for task in tasks),
        degraded_count=sum(task.status == "degraded" for task in tasks),
        failed_count=sum(task.status in {"failed", "canceled"} for task in tasks),
        timeout_count=sum(task.status == "timed_out" for task in tasks),
        needs_review_count=sum(artifact.needs_review for artifact in artifacts),
        is_current=run.is_current,
        artifacts=[_artifact_summary(artifact) for artifact in artifacts],
        context_fingerprint=run.context_fingerprint or "",
        catalog_version=run.catalog_version,
        template_versions=template_versions,
        data_asset_versions=data_asset_versions,
        renderer_versions=renderer_versions,
        marker=_mode_marker(run.production_mode),
    )


def _workflow_input(run: ProductionRun) -> ArtifactProductionWorkflowInput:
    return ArtifactProductionWorkflowInput(
        production_run_id=str(run.id),
        assessment_run_id=str(run.assessment_run_id or ""),
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        deadline_at=run.deadline_at.isoformat(),
        catalog_version=run.catalog_version,
        context_fingerprint=run.context_fingerprint or "",
        launch_mode=run.launch_mode,
        generation_seq=run.generation_seq,
        generation_scope=run.generation_scope,
        required_outputs=tuple(
            (item["artifact_key"], item["output_profile"])
            for item in run.required_outputs
        ),
    )


def _map_artifact_read_error(error: Exception) -> HTTPException:
    if isinstance(error, SQLAlchemyError):
        return HTTPException(
            status_code=503,
            detail="artifact storage is unavailable",
        )
    raise error


@router.get(
    "/assessments/runs/{assessment_run_id}/production",
    response_model=ProductionRunResponse,
)
async def get_assessment_production(
    assessment_run_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> ProductionRunResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await session.scalar(
                    select(ProductionRun)
                    .where(
                        ProductionRun.assessment_run_id == assessment_run_id,
                        ProductionRun.generation_scope == "full",
                    )
                    .order_by(
                        ProductionRun.generation_seq.desc(),
                        ProductionRun.created_at.desc(),
                    )
                    .limit(1)
                )
                if run is None:
                    raise LookupError("artifact_production_run_not_found")
                return await _production_run_response(session, run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _map_artifact_read_error(exc) from exc


@router.get(
    "/artifact-production-runs/{production_run_id}",
    response_model=ProductionRunResponse,
)
async def get_artifact_production_run(
    production_run_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> ProductionRunResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await session.get(ProductionRun, production_run_id)
                if run is None:
                    raise LookupError("artifact_production_run_not_found")
                return await _production_run_response(session, run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _map_artifact_read_error(exc) from exc


@router.get(
    "/events/{event_id}/artifacts",
    response_model=list[ArtifactSummaryResponse],
)
async def list_event_artifacts(
    event_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> list[ArtifactSummaryResponse]:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                event = await session.get(EarthquakeEvent, event_id)
                if event is None:
                    raise LookupError("artifact_event_not_found")
                rows = await session.scalars(
                    select(GeneratedArtifact)
                    .join(
                        ArtifactPublication,
                        ArtifactPublication.artifact_id == GeneratedArtifact.id,
                    )
                    .where(
                        ArtifactPublication.event_id == event.id,
                        ArtifactPublication.superseded_at.is_(None),
                    )
                    .order_by(
                        GeneratedArtifact.artifact_key,
                        GeneratedArtifact.output_profile,
                    )
                )
                return [_artifact_summary(artifact) for artifact in rows]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _map_artifact_read_error(exc) from exc


@router.get(
    "/events/{event_id}/artifact-versions",
    response_model=list[ArtifactSummaryResponse],
)
async def list_event_artifact_versions(
    event_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> list[ArtifactSummaryResponse]:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                event = await session.get(EarthquakeEvent, event_id)
                if event is None:
                    raise LookupError("artifact_event_not_found")
                rows = await session.scalars(
                    select(GeneratedArtifact)
                    .where(
                        GeneratedArtifact.event_id == event.id,
                        GeneratedArtifact.is_final.is_(True),
                    )
                    .order_by(
                        GeneratedArtifact.artifact_key,
                        GeneratedArtifact.output_profile,
                        GeneratedArtifact.artifact_version.desc(),
                    )
                )
                return [_artifact_summary(artifact) for artifact in rows]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _map_artifact_read_error(exc) from exc


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ArtifactSummaryResponse,
)
async def get_artifact(
    artifact_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> ArtifactSummaryResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                artifact = await session.get(GeneratedArtifact, artifact_id)
                if artifact is None:
                    raise LookupError("artifact_not_found")
                return _artifact_summary(artifact)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _map_artifact_read_error(exc) from exc


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> FileResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                artifact = await session.get(GeneratedArtifact, artifact_id)
                if artifact is None:
                    raise LookupError("artifact_not_found")
                path = ArtifactStore(settings.artifact_storage_root).resolve(
                    artifact.storage_path
                )
                if not path.is_file():
                    raise LookupError("artifact_file_not_found")
                return FileResponse(
                    path,
                    media_type=_media_type(artifact.format),
                    filename=Path(artifact.file_name).name,
                    content_disposition_type="attachment",
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="artifact storage is unavailable",
        ) from exc


@router.get("/artifacts/{artifact_id}/thumbnail")
async def get_artifact_thumbnail(
    artifact_id: UUID,
    _current_user: object = Depends(require_role(*_ARTIFACT_READ_ROLES)),
) -> FileResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                artifact = await session.get(GeneratedArtifact, artifact_id)
                if artifact is None:
                    raise LookupError("artifact_not_found")
                if not _has_thumbnail(artifact.format):
                    raise LookupError("artifact_thumbnail_not_found")
                path = ArtifactStore(settings.artifact_storage_root).resolve(
                    artifact.storage_path
                )
                if not path.is_file():
                    raise LookupError("artifact_file_not_found")
                return FileResponse(
                    path,
                    media_type=_media_type(artifact.format),
                    filename=Path(artifact.file_name).name,
                    content_disposition_type="inline",
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="artifact storage is unavailable",
        ) from exc


@router.post(
    "/events/{event_id}/artifacts/{artifact_key}/rebuild",
    response_model=ProductionRunResponse,
    status_code=202,
)
async def rebuild_artifact(
    event_id: UUID,
    artifact_key: str,
    rebuild_request: ArtifactRebuildRequest,
    output_profile: str = Query(default="a3v-professional"),
    current_user: AuthUser = Depends(get_current_user),
    service: ArtifactProductionService = Depends(get_artifact_production_service),
    starter: ArtifactWorkflowStarter = Depends(get_artifact_workflow_starter),
) -> ProductionRunResponse:
    if not can_rebuild_artifacts(current_user):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        async with SessionFactory() as session:
            async with session.begin():
                event = await session.get(EarthquakeEvent, event_id)
                if event is None:
                    raise LookupError("artifact_event_not_found")
                revision_id = event.current_revision_id
                if revision_id is None:
                    raise LookupError("artifact_revision_not_found")

        prepared = await service.create_rebuild_run(
            event_id=event_id,
            revision_id=revision_id,
            artifact_key=artifact_key,
            output_profile=output_profile,
            requested_by=current_user.username,
            reason=rebuild_request.reason,
        )
        async with SessionFactory() as session:
            async with session.begin():
                run = await session.get(ProductionRun, prepared.production_run_id)
                if run is None:
                    raise LookupError("artifact_production_run_not_found")
                await starter.start(_workflow_input(run))
                return await _production_run_response(session, run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="artifact storage is unavailable",
        ) from exc


@router.post(
    "/events/{event_id}/artifacts/{artifact_key}/override",
    response_model=dict[str, object],
    status_code=201,
)
async def override_artifact(
    event_id: UUID,
    artifact_key: str,
    file: UploadFile = File(...),
    revision_id: UUID = Form(...),
    reason: str = Form(...),
    expected_current_artifact_id: UUID | None = Form(default=None),
    output_profile: str = Query(default="a3v-professional"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_user: AuthUser = Depends(require_role("superadmin")),
    service: ArtifactOverrideService = Depends(get_artifact_override_service),
) -> dict[str, object]:
    try:
        uuid.UUID(idempotency_key)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key must be a UUID",
        ) from exc
    try:
        response = await service.override(
            actor_id=current_user.username,
            event_id=event_id,
            artifact_key=artifact_key,
            output_profile=output_profile,
            revision_id=revision_id,
            reason=reason,
            expected_current_artifact_id=expected_current_artifact_id,
            idempotency_key=idempotency_key,
            upload=file.file,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArtifactOverrideValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{exc.error_category}: {exc.summary}",
        ) from exc
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="artifact storage is unavailable",
        ) from exc
    return response.to_body()
