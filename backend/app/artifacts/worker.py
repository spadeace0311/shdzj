from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
import signal
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from app.artifacts.catalog import load_catalog
from app.artifacts.context import ProductionContextService
from app.artifacts.domain import ArtifactKind, DependencyKind
from app.artifacts.dispatcher import (
    ArtifactProductionDispatcher,
    TemporalArtifactCancellationStarter,
)
from app.artifacts.models import GeneratedArtifact, ProductionRun, ProductionTask
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
    _effective_now,
)
from app.artifacts.retention import ArtifactRetentionService
from app.artifacts.renderers.base import (
    LocalAssetMissingError,
    RemoteAssetForbiddenError,
    RenderResult,
)
from app.artifacts.renderers.docx_renderer import (
    DocxRenderer,
    build_background_document_spec,
    build_core_document_spec,
)
from app.artifacts.renderers.map_renderer import (
    BrowserPool,
    MapRenderer,
    MapSpecBuilder,
)
from app.artifacts.renderers.pptx_renderer import PptxRenderer
from app.artifacts.service import ArtifactProductionService
from app.artifacts.storage import ArtifactStore
from app.artifacts.validation import ArtifactValidator
from app.artifacts.workflow import (
    ArtifactDependencyWaitInput,
    ArtifactProductionDeadlineInput,
    ArtifactProductionWorkflow,
    ArtifactProductionWorkflowInput,
    ArtifactPublicationInput,
    ArtifactTaskActivityInput,
    ArtifactTerminalizationInput,
    ArtifactValidationInput,
)
from app.config import Settings, settings
from app.db import SessionFactory
from app.events.models import EarthquakeEvent, EarthquakeRevision
from sqlalchemy import select

logger = logging.getLogger(__name__)

_DEPENDENCY_WAIT_SECONDS = 0.5
_ACTIVITY_CONCURRENCY = 128


class ArtifactActivities:
    def __init__(
        self,
        session_factory: object,
        *,
        render_concurrency: int | None = None,
        context_service: ProductionContextService | None = None,
        store: ArtifactStore | None = None,
        validator: ArtifactValidator | None = None,
        renderer_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._context_service = context_service
        self._store = store
        self._validator = validator
        self._renderer_factory = renderer_factory
        self._render_slots = asyncio.Semaphore(
            render_concurrency
            if render_concurrency is not None
            else settings.artifact_render_concurrency
        )
        self._dependency_prepare_lock = asyncio.Lock()

    @property
    def _context(self) -> ProductionContextService:
        if self._context_service is None:
            self._context_service = ProductionContextService()
        return self._context_service

    @property
    def _artifact_store(self) -> ArtifactStore:
        if self._store is None:
            self._store = ArtifactStore(settings.artifact_storage_root)
        return self._store

    @property
    def _artifact_validator(self) -> ArtifactValidator:
        if self._validator is None:
            self._validator = ArtifactValidator()
        return self._validator

    @activity.defn(name="prepare_artifact_production")
    async def prepare_artifact_production(
        self,
        request: ArtifactProductionWorkflowInput,
    ):
        catalog = load_catalog(settings.artifact_catalog_path)
        if request.catalog_version and request.catalog_version != catalog.catalog_version:
            raise ApplicationError(
                "production catalog version changed",
                type="catalog_version_changed",
                non_retryable=True,
            )
        repository = ArtifactProductionRepository(catalog)
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(ProductionRun, request.production_run_id)
                event = await session.get(EarthquakeEvent, request.event_id)
                revision = await session.get(EarthquakeRevision, request.revision_id)
                if run is None or event is None or revision is None:
                    raise LookupError("artifact production inputs were not found")
                if run.event_id != event.id or run.revision_id != revision.id:
                    raise ValueError("production run inputs do not match")
                if run.status == "pending":
                    run.status = "running"
                    run.started_at = datetime.now(UTC)
                frozen_context = await self._context.freeze_static_context(
                    session,
                    run.id,
                    catalog,
                )
                run.context_fingerprint = frozen_context.context_fingerprint
                await repository.prepare_dependencies(session, run.id)
                tasks = await repository.list_tasks(session, run.id)
                groups = _phase_groups_from_tasks(tasks)
                await session.flush()
                return {
                    "production_run_id": str(run.id),
                    "deadline_at": run.deadline_at,
                    "deadline_basis_at": run.deadline_basis_at,
                    "clock_started_at": run.started_at,
                    "context_fingerprint": run.context_fingerprint or "",
                    "launch_mode": run.launch_mode,
                    "background_outputs": groups["background"],
                    "intensity_outputs": groups["intensity"],
                    "loss_core_outputs": groups["loss_core"],
                    "loss_final_outputs": groups["loss_final"],
                }

    @activity.defn(name="wait_for_artifact_dependencies")
    async def wait_for_artifact_dependencies(
        self,
        request: ArtifactDependencyWaitInput,
    ):
        repository = ArtifactProductionRepository()
        deadline = _as_utc(request.deadline_at, "deadline_at")
        while True:
            ready_task_id: uuid.UUID | None = None
            ready_run_id: uuid.UUID | None = None
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.scalar(
                        select(ProductionTask).where(
                            ProductionTask.production_run_id
                            == request.production_run_id,
                            ProductionTask.artifact_key == request.artifact_key,
                            ProductionTask.output_profile
                            == request.output_profile,
                        )
                    )
                    if task is None:
                        raise LookupError("artifact production task not found")
                    run = await session.get(ProductionRun, task.production_run_id)
                    if run is None:
                        raise LookupError("artifact production run not found")
                    if task.status == "pending":
                        async with self._dependency_prepare_lock:
                            await repository.prepare_dependencies(session, run.id)
                        await session.refresh(task)

                    if task.status == "ready":
                        ready_task_id = task.id
                        ready_run_id = run.id

                    elif task.status in {"succeeded", "degraded"}:
                        if not task.input_fingerprint:
                            raise ValueError(
                                "completed task has no input fingerprint"
                            )
                        return ArtifactTaskActivityInput(
                            production_run_id=str(run.id),
                            production_task_id=str(task.id),
                            artifact_key=task.artifact_key,
                            output_profile=task.output_profile,
                            context_fingerprint=run.context_fingerprint or "",
                            input_fingerprint=task.input_fingerprint,
                            deadline_at=task.deadline_at.isoformat(),
                            already_completed=True,
                        )

                    elif task.status in {
                        "failed",
                        "timed_out",
                        "canceled",
                    }:
                        raise ApplicationError(
                            f"artifact task is {task.status}",
                            type="artifact_dependency_unavailable",
                            non_retryable=True,
                        )
                    else:
                        observed = _effective_now(
                            run,
                            datetime.now(UTC),
                        )
                        if await _assessment_failed_for_run(session, run):
                            await repository.fail_task(
                                session,
                                task.id,
                                "assessment_unavailable",
                                "parent assessment failed before dependencies were available",
                            )
                            raise ApplicationError(
                                "assessment unavailable",
                                type="assessment_unavailable",
                                non_retryable=True,
                            )
                        if observed >= deadline:
                            await repository.fail_task(
                                session,
                                task.id,
                                "deadline_exceeded",
                                "artifact dependency wait exceeded production deadline",
                            )
                            raise ApplicationError(
                                "artifact dependency wait exceeded production deadline",
                                type="deadline_exceeded",
                                non_retryable=True,
                            )
            if ready_task_id is not None and ready_run_id is not None:
                service = ArtifactProductionService(
                    session_factory=self._session_factory,
                    repository=repository,
                )
                try:
                    prepared = await service.prepare_task(ready_task_id)
                except ValueError as exc:
                    raise ApplicationError(
                        str(exc),
                        type="artifact_dependency_unavailable",
                        non_retryable=True,
                    ) from exc
                idempotency_key = _idempotency_key(
                    ready_run_id,
                    prepared.artifact_key,
                    prepared.output_profile,
                    prepared.input_fingerprint,
                )
                async with self._session_factory() as session:
                    async with session.begin():
                        started = await repository.start_task(
                            session,
                            ready_task_id,
                            idempotency_key,
                        )
                        run = await session.get(ProductionRun, ready_run_id)
                        if run is None:
                            raise LookupError(
                                "artifact production run not found"
                            )
                        return ArtifactTaskActivityInput(
                            production_run_id=str(run.id),
                            production_task_id=str(started.id),
                            artifact_key=started.artifact_key,
                            output_profile=started.output_profile,
                            context_fingerprint=run.context_fingerprint or "",
                            input_fingerprint=started.input_fingerprint or "",
                            deadline_at=started.deadline_at.isoformat(),
                        )
            await asyncio.sleep(_DEPENDENCY_WAIT_SECONDS)

    @activity.defn(name="render_map_artifact")
    async def render_map_artifact(self, request: ArtifactTaskActivityInput):
        pool: _TrackingBrowserPool | None = None
        staged: Path | None = None
        try:
            async with self._render_slots:
                pool = _TrackingBrowserPool()
                renderer = MapRenderer(pool)
                staged, official_name, render_result = await self._render_map(
                    request,
                    renderer,
                )
                stored = self._artifact_store.store_immutable(
                    staged,
                    file_name=official_name,
                )
                try:
                    await self._commit_rendered_artifact(
                        request,
                        stored.file_name,
                        stored.relative_path,
                        stored.checksum,
                        stored.size_bytes,
                        render_result,
                    )
                finally:
                    staged.unlink(missing_ok=True)
                return {
                    "production_run_id": request.production_run_id,
                    "production_task_id": request.production_task_id,
                    "artifact_key": request.artifact_key,
                    "output_profile": request.output_profile,
                    "checksum": stored.checksum,
                }
        except asyncio.CancelledError:
            if pool is not None:
                await pool.cancel_all()
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._cancel_task(request)
            raise
        except BaseException as exc:
            if pool is not None:
                await pool.cancel_all()
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._fail_task_with_error(request, exc)
            raise _typed_application_error(exc) from exc
        finally:
            if pool is not None:
                await pool.close()

    @activity.defn(name="compose_docx_artifact")
    async def compose_docx_artifact(self, request: ArtifactTaskActivityInput):
        staged: Path | None = None
        try:
            async with self._render_slots:
                staged, official_name, render_result = await self._render_document(
                    request,
                    ArtifactKind.DOCX,
                )
                stored = self._artifact_store.store_immutable(
                    staged,
                    file_name=official_name,
                )
                try:
                    await self._commit_rendered_artifact(
                        request,
                        stored.file_name,
                        stored.relative_path,
                        stored.checksum,
                        stored.size_bytes,
                        render_result,
                    )
                finally:
                    staged.unlink(missing_ok=True)
                return {
                    "production_run_id": request.production_run_id,
                    "production_task_id": request.production_task_id,
                    "artifact_key": request.artifact_key,
                    "output_profile": request.output_profile,
                    "checksum": stored.checksum,
                }
        except asyncio.CancelledError:
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._cancel_task(request)
            raise
        except BaseException as exc:
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._fail_task_with_error(request, exc)
            raise _typed_application_error(exc) from exc

    @activity.defn(name="compose_pptx_artifact")
    async def compose_pptx_artifact(self, request: ArtifactTaskActivityInput):
        staged: Path | None = None
        try:
            async with self._render_slots:
                staged, official_name, render_result = await self._render_document(
                    request,
                    ArtifactKind.PPTX,
                )
                stored = self._artifact_store.store_immutable(
                    staged,
                    file_name=official_name,
                )
                try:
                    await self._commit_rendered_artifact(
                        request,
                        stored.file_name,
                        stored.relative_path,
                        stored.checksum,
                        stored.size_bytes,
                        render_result,
                    )
                finally:
                    staged.unlink(missing_ok=True)
                return {
                    "production_run_id": request.production_run_id,
                    "production_task_id": request.production_task_id,
                    "artifact_key": request.artifact_key,
                    "output_profile": request.output_profile,
                    "checksum": stored.checksum,
                }
        except asyncio.CancelledError:
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._cancel_task(request)
            raise
        except BaseException as exc:
            if staged is not None:
                staged.unlink(missing_ok=True)
            await self._fail_task_with_error(request, exc)
            raise _typed_application_error(exc) from exc

    @activity.defn(name="validate_artifact_production")
    async def validate_artifact_production(
        self,
        request: ArtifactValidationInput,
    ):
        repository = ArtifactProductionRepository()
        observed = _as_utc(request.observed_at, "observed_at")
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(ProductionRun, request.production_run_id)
                if run is None:
                    raise LookupError("artifact production run not found")
                artifacts = (
                    await session.scalars(
                        select(GeneratedArtifact).where(
                            GeneratedArtifact.production_run_id == run.id,
                            GeneratedArtifact.is_final.is_(True),
                        )
                    )
                ).all()
                try:
                    for artifact_row in artifacts:
                        path = self._artifact_store.resolve(
                            artifact_row.storage_path
                        )
                        definition = load_catalog(
                            settings.artifact_catalog_path
                        ).get(artifact_row.artifact_key, artifact_row.output_profile)
                        result = self._artifact_validator.validate(
                            path,
                            definition,
                            artifact_row.production_mode,
                        )
                        if not result.valid:
                            raise ApplicationError(
                                result.summary,
                                type=result.error_category or "validation_failed",
                                non_retryable=True,
                            )
                except ApplicationError as exc:
                    await repository.finalize_run_with_failure(
                        session,
                        run.id,
                        observed,
                        exc.type or "validation_failed",
                        str(exc),
                    )
                    raise ApplicationError(
                        str(exc),
                        type=exc.type or "validation_failed",
                        non_retryable=True,
                    ) from exc
                tasks = await repository.list_tasks(session, run.id)
                return {
                    "status": "validated",
                    "completed_count": sum(
                        task.status in {"succeeded", "degraded"}
                        for task in tasks
                    ),
                    "failed_count": sum(
                        task.status in {"failed", "timed_out", "canceled"}
                        for task in tasks
                    ),
                }

    @activity.defn(name="publish_artifact_production")
    async def publish_artifact_production(
        self,
        request: ArtifactPublicationInput,
    ):
        repository = ArtifactProductionRepository()
        observed = _as_utc(request.observed_at, "observed_at")
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(ProductionRun, request.production_run_id)
                if run is None:
                    raise LookupError("artifact production run not found")
                artifacts = (
                    await session.scalars(
                        select(GeneratedArtifact).where(
                            GeneratedArtifact.production_run_id == run.id,
                            GeneratedArtifact.is_final.is_(True),
                        )
                    )
                ).all()
                publication_failures: list[str] = []
                for artifact_row in artifacts:
                    try:
                        await repository.publish_artifact(
                            session,
                            artifact_row.id,
                            published_by=request.published_by,
                            forced=request.forced,
                        )
                    except (ValueError, RuntimeError) as exc:
                        category = (
                            "publication_concurrency"
                            if isinstance(exc, RuntimeError)
                            else "publication_conflict"
                        )
                        await repository.record_publication_failure(
                            session,
                            artifact_row.production_task_id,
                            category,
                            str(exc),
                            observed,
                        )
                        publication_failures.append(artifact_row.artifact_key)
                        logger.warning(
                            "artifact publish failed run_id=%s artifact_id=%s error=%s",
                            run.id,
                            artifact_row.id,
                            exc,
                        )
                finalized = await repository.finalize_run(
                    session,
                    run.id,
                    observed,
                )
                tasks = await repository.list_tasks(session, run.id)
                return {
                    "status": finalized.status,
                    "completed_count": sum(
                        task.status in {"succeeded", "degraded"}
                        for task in tasks
                    ),
                    "failed_count": sum(
                        task.status in {"failed", "timed_out", "canceled"}
                        for task in tasks
                    ),
                    "publication_failed_count": len(publication_failures),
                    "publication_status": (
                        "completed"
                        if not publication_failures
                        else (
                            "partial"
                            if len(publication_failures) < len(artifacts)
                            else "failed"
                        )
                    ),
                }

    @activity.defn(name="mark_production_deadline_exceeded")
    async def mark_production_deadline_exceeded(
        self,
        request: ArtifactProductionDeadlineInput,
    ):
        repository = ArtifactProductionRepository()
        observed = _as_utc(request.observed_at, "observed_at")
        async with self._session_factory() as session:
            async with session.begin():
                await repository.timeout_run(
                    session,
                    request.production_run_id,
                    observed,
                )
                return None

    @activity.defn(name="terminalize_artifact_production")
    async def terminalize_artifact_production(
        self,
        request: ArtifactTerminalizationInput,
    ):
        repository = ArtifactProductionRepository()
        observed = _as_utc(request.observed_at, "observed_at")
        if request.reason == "assessment_failed":
            status = "failed"
            category = "assessment_unavailable"
        elif request.reason == "deadline":
            status = "timed_out"
            category = "deadline_exceeded"
        else:
            status = "failed"
            category = "production_failed"
        async with self._session_factory() as session:
            async with session.begin():
                run = await repository.terminalize_unfinished_tasks(
                    session,
                    request.production_run_id,
                    observed,
                    status=status,
                    category=category,
                    summary=request.summary,
                )
                if run.status not in {
                    "completed",
                    "partial",
                    "failed",
                    "canceled",
                }:
                    await repository.finalize_run(
                        session,
                        run.id,
                        observed,
                    )
                tasks = await repository.list_tasks(session, run.id)
                return {
                    "status": run.status,
                    "completed_count": sum(
                        task.status in {"succeeded", "degraded"}
                        for task in tasks
                    ),
                    "failed_count": sum(
                        task.status in {"failed", "timed_out", "canceled"}
                        for task in tasks
                    ),
                }

    @activity.defn(name="cancel_artifact_production")
    async def cancel_artifact_production(
        self,
        request: ArtifactTerminalizationInput,
    ):
        repository = ArtifactProductionRepository()
        observed = _as_utc(request.observed_at, "observed_at")
        async with self._session_factory() as session:
            async with session.begin():
                run = await repository.cancel_run(
                    session,
                    request.production_run_id,
                    observed,
                    request.summary or request.reason,
                )
                tasks = await repository.list_tasks(session, run.id)
                return {
                    "status": run.status,
                    "completed_count": sum(
                        task.status in {"succeeded", "degraded"}
                        for task in tasks
                    ),
                    "failed_count": sum(
                        task.status in {"failed", "timed_out", "canceled"}
                        for task in tasks
                    ),
                }

    async def _render_map(
        self,
        request: ArtifactTaskActivityInput,
        renderer: MapRenderer,
    ) -> tuple[Path, str, RenderResult]:
        async with self._session_factory() as session:
            context = await self._context.build_map_context(
                session,
                request.production_task_id,
            )
        spec = MapSpecBuilder().build(context)
        suffix = ".jpg" if spec.output.format == "jpg" else ".png"
        descriptor, name = tempfile.mkstemp(
            prefix=f"{request.artifact_key}-",
            suffix=suffix,
        )
        os.close(descriptor)
        Path(name).unlink(missing_ok=True)
        output = Path(name)
        result = await renderer.render(spec, output)
        return result.path, result.file_name, result

    async def _render_document(
        self,
        request: ArtifactTaskActivityInput,
        kind: ArtifactKind,
    ) -> tuple[Path, str, RenderResult]:
        async with self._session_factory() as session:
            context = await self._context.build_document_context(
                session,
                request.production_task_id,
                kind.value,
            )
        catalog = load_catalog(settings.artifact_catalog_path)
        definition = catalog.get(request.artifact_key, request.output_profile)
        if definition.template_package == "background-template":
            spec = build_background_document_spec(
                context,
                request.artifact_key,
            )
        else:
            spec = build_core_document_spec(context, request.artifact_key)
        suffix = ".pptx" if kind == ArtifactKind.PPTX else ".docx"
        descriptor, name = tempfile.mkstemp(
            prefix=f"{request.artifact_key}-",
            suffix=suffix,
        )
        os.close(descriptor)
        Path(name).unlink(missing_ok=True)
        output = Path(name)
        renderer = (
            PptxRenderer()
            if kind == ArtifactKind.PPTX
            else DocxRenderer()
        )
        result = await renderer.render(spec, output)
        return result.path, result.file_name, result

    async def _commit_rendered_artifact(
        self,
        request: ArtifactTaskActivityInput,
        file_name: str,
        storage_path: str,
        checksum: str,
        size_bytes: int,
        render_result: RenderResult,
    ) -> None:
        quality = render_result.quality
        marker = render_result.render_manifest.get("marker")
        result = ArtifactGenerationResult(
            file_name=file_name,
            format=Path(file_name).suffix.lstrip("."),
            storage_path=storage_path,
            checksum=checksum,
            size_bytes=size_bytes,
            quality=ArtifactQuality(
                grade=quality.grade,
                needs_review=quality.needs_review,
                degradation_reasons=quality.degradation_reasons,
            ),
            marker={"marker": marker} if marker is not None else None,
            width=(
                render_result.width if render_result.width > 0 else None
            ),
            height=(
                render_result.height if render_result.height > 0 else None
            ),
            page_count=render_result.page_count,
            render_manifest=render_result.render_manifest,
            generated_at=render_result.generated_at or datetime.now(UTC),
            task_status=render_result.task_status,
        )
        async with self._session_factory() as session:
            async with session.begin():
                await ArtifactProductionRepository().complete_task(
                    session,
                    request.production_task_id,
                    result,
                    render_result.task_status,
                )

    async def _cancel_task(self, request: ArtifactTaskActivityInput) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await ArtifactProductionRepository().cancel_task(
                    session,
                    request.production_task_id,
                    "artifact activity canceled",
                )

    async def _fail_task_with_error(
        self,
        request: ArtifactTaskActivityInput,
        error: BaseException,
    ) -> None:
        category, summary = _error_category_and_summary(error)
        async with self._session_factory() as session:
            async with session.begin():
                try:
                    await ArtifactProductionRepository().fail_task(
                        session,
                        request.production_task_id,
                        category,
                        summary,
                    )
                except ValueError:
                    pass


def _phase_groups_from_tasks(
    tasks: list[ProductionTask],
) -> dict[str, tuple[tuple[str, str], ...]]:
    background: list[tuple[str, str]] = []
    intensity: list[tuple[str, str]] = []
    loss_core: list[tuple[str, str]] = []
    loss_final: list[tuple[str, str]] = []
    core_keys = {"loss.buildings", "loss.population"}
    final_keys = {
        "loss.casualties",
        "loss.economic",
        "loss.resources",
        "loss.validate",
    }
    for task in tasks:
        output = (task.artifact_key, task.output_profile)
        hard_product_keys = {
            str(dep.get("key"))
            for dep in (task.depends_on or ())
            if isinstance(dep, dict)
            and dep.get("kind") == DependencyKind.ASSESSMENT_PRODUCT.value
        }
        if "intensity.fusion" in hard_product_keys:
            intensity.append(output)
        elif hard_product_keys & core_keys:
            loss_core.append(output)
        elif hard_product_keys & final_keys:
            loss_final.append(output)
        else:
            background.append(output)
    return {
        "background": tuple(background),
        "intensity": tuple(intensity),
        "loss_core": tuple(loss_core),
        "loss_final": tuple(loss_final),
    }


def _idempotency_key(
    production_run_id: Any,
    artifact_key: str,
    output_profile: str,
    fingerprint: str,
) -> str:
    return (
        f"artifact:{production_run_id}:{artifact_key}:"
        f"{output_profile}:{fingerprint}"
    )


async def _assessment_failed_for_run(session: object, run: ProductionRun) -> bool:
    if run.assessment_run_id is None:
        return False
    from app.assessment.models import AssessmentRun

    status = await session.scalar(
        select(AssessmentRun.status).where(
            AssessmentRun.id == run.assessment_run_id
        )
    )
    return status == "failed"


def _as_utc(value: datetime | str, field: str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value.astimezone(UTC)


def _error_category_and_summary(error: BaseException) -> tuple[str, str]:
    if isinstance(error, LocalAssetMissingError):
        return "local_asset_missing", str(error)[:2000]
    if isinstance(error, RemoteAssetForbiddenError):
        return "remote_asset_forbidden", str(error)[:2000]
    if isinstance(error, ValueError):
        return "invalid_render_input", str(error)[:2000]
    if isinstance(error, LookupError):
        return "artifact_dependency_unavailable", str(error)[:2000]
    return "artifact_render_failed", str(error)[:2000]


def _typed_application_error(error: BaseException) -> ApplicationError:
    if isinstance(error, ApplicationError):
        return error
    category, summary = _error_category_and_summary(error)
    return ApplicationError(
        summary,
        type=category,
        non_retryable=True,
    )


class _TrackingBrowserPool(BrowserPool):
    def __init__(self) -> None:
        super().__init__()
        self._tokens: set[str] = set()

    async def acquire_page(self, priority: int = 100):
        slot = await super().acquire_page(priority)
        self._tokens.add(slot.token)
        return slot

    async def release_slot(self, token: str) -> None:
        self._tokens.discard(token)
        await super().release_slot(token)

    async def cancel_all(self) -> None:
        for token in tuple(self._tokens):
            await self.cancel(token)


def build_artifact_worker(
    *,
    client: Client,
    session_factory: object,
    configured: Settings = settings,
) -> Worker:
    activities = ArtifactActivities(
        session_factory,
        render_concurrency=configured.artifact_render_concurrency,
    )
    return Worker(
        client,
        task_queue=configured.temporal_task_queue,
        workflows=[ArtifactProductionWorkflow],
        activities=[
            activities.prepare_artifact_production,
            activities.wait_for_artifact_dependencies,
            activities.render_map_artifact,
            activities.compose_docx_artifact,
            activities.compose_pptx_artifact,
            activities.validate_artifact_production,
            activities.publish_artifact_production,
            activities.terminalize_artifact_production,
            activities.cancel_artifact_production,
            activities.mark_production_deadline_exceeded,
        ],
        disable_eager_activity_execution=True,
        max_concurrent_activities=_ACTIVITY_CONCURRENCY,
        graceful_shutdown_timeout=timedelta(seconds=10),
    )


async def run_artifact_worker(
    stop_event: asyncio.Event | None = None,
    *,
    client: Client | None = None,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
) -> None:
    client = client or await _connect_temporal(configured)
    worker = build_artifact_worker(
        client=client,
        session_factory=session_factory,
        configured=configured,
    )
    retention_task = asyncio.create_task(
        _run_retention_loop(
            session_factory=session_factory,
            configured=configured,
        )
    )
    try:
        if stop_event is None:
            await worker.run()
        else:
            await _run_until_stopped(worker.run(), stop_event)
    finally:
        retention_task.cancel()
        await asyncio.gather(retention_task, return_exceptions=True)


async def _run_retention_loop(
    *,
    session_factory: object,
    configured: Settings,
) -> None:
    if not configured.artifact_retention_enabled:
        return
    service = ArtifactRetentionService()
    while True:
        await asyncio.sleep(configured.artifact_retention_interval_seconds)
        observed_at = datetime.now(UTC)
        try:
            async with session_factory() as session:
                async with session.begin():
                    result = await service.retain_expired(
                        session,
                        observed_at=observed_at,
                    )
            payload = {
                "test": result.test_deleted,
                "drill": result.drill_deleted,
                "live": result.live_deleted,
                "manual": result.manual_deleted,
                "replay": result.replay_deleted,
                "failed": result.failed_deletions,
                "protected_publications": result.protected_publication_count,
                "protection_reasons": dict(
                    result.protected_publication_reasons
                ),
            }
            logger.info(
                "artifact retention completed counts=%s",
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                extra={
                    "artifact_retention_counts": payload,
                },
            )
        except Exception:
            logger.exception("artifact retention failed")


async def run_artifact_dispatcher(
    stop_event: asyncio.Event | None = None,
    *,
    client: Client | None = None,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    stop_event = stop_event or asyncio.Event()
    client = client or await _connect_temporal(configured)
    logger.info("artifact production dispatcher connected")
    dispatcher = ArtifactProductionDispatcher(
        session_factory=session_factory,
        starter=TemporalArtifactCancellationStarter(client),
        batch_size=configured.assessment_outbox_batch_size,
        max_attempts=configured.assessment_outbox_max_attempts,
        lease_seconds=configured.assessment_outbox_lease_seconds,
    )
    while not stop_event.is_set():
        try:
            await dispatcher.dispatch_once()
        except Exception:
            logger.exception("artifact production cancellation dispatch failed")
        await _wait_for_stop_or_sleep(
            stop_event,
            configured.assessment_outbox_poll_seconds,
            sleep,
        )


async def _wait_for_stop_or_sleep(
    stop_event: asyncio.Event,
    delay: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    if stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        await sleep(0)


async def _connect_temporal(configured: Settings) -> Client:
    return await Client.connect(
        configured.temporal_address,
        namespace=configured.temporal_namespace,
    )


async def _run_until_stopped(
    worker_run: Awaitable[None],
    stop_event: asyncio.Event,
) -> None:
    worker_task = asyncio.create_task(worker_run)
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {worker_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if worker_task in done:
        await worker_task


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())


async def _run_process(mode: str) -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    if mode == "dispatcher":
        await run_artifact_dispatcher(stop_event)
    else:
        await run_artifact_worker(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("process", choices=("dispatcher", "worker"))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )
    try:
        asyncio.run(_run_process(args.process))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
