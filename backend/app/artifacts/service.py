from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.artifacts.domain import ArtifactCatalog
from app.artifacts.models import (
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    CreateProductionRunCommand,
    PreparedProductionRun,
)
from app.assessment.models import AssessmentRun
from app.events.models import EarthquakeEvent, EarthquakeRevision


@dataclass(frozen=True, slots=True)
class ArtifactRenderInput:
    production_run_id: uuid.UUID
    production_task_id: uuid.UUID
    artifact_key: str
    output_profile: str
    context_fingerprint: str
    input_fingerprint: str
    deadline_at: datetime
    dependency_bindings: tuple[ArtifactTaskDependencyBinding, ...]


class ArtifactProductionService:
    """Coordinates repository operations without owning caller transactions."""

    def __init__(
        self,
        *,
        session_factory: object,
        repository: ArtifactProductionRepository | None = None,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or ArtifactProductionRepository(catalog)
        self._catalog = catalog or self._repository.catalog

    async def create_initial_run(
        self,
        *,
        assessment_run_id: uuid.UUID,
        event_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> PreparedProductionRun:
        async with self._session_factory() as session:
            async with session.begin():
                assessment_run = await session.get(
                    AssessmentRun,
                    assessment_run_id,
                )
                event = await session.get(EarthquakeEvent, event_id)
                revision = await session.get(EarthquakeRevision, revision_id)
                if assessment_run is None or event is None or revision is None:
                    raise LookupError("artifact production inputs were not found")
                if (
                    assessment_run.event_id != event.id
                    or assessment_run.revision_id != revision.id
                ):
                    raise ValueError(
                        "assessment run does not match event and revision"
                    )
                required_outputs = self._catalog.full_required_outputs()
                command = CreateProductionRunCommand(
                    assessment_run_id=assessment_run.id,
                    event_id=event.id,
                    revision_id=revision.id,
                    revision_no=revision.revision_no,
                    production_mode=_production_mode(revision.revision_kind),
                    launch_mode="assessment_child",
                    deadline_basis_at=assessment_run.deadline_basis_at,
                    deadline_at=assessment_run.deadline_at,
                    deadline_kind="event_deadline",
                    catalog_version=self._catalog.catalog_version,
                    generation_scope="full",
                    required_outputs=required_outputs,
                    snapshot={
                        "launch": "assessment_child",
                        "event_id": str(event.id),
                        "revision_id": str(revision.id),
                        "revision_no": revision.revision_no,
                        "assessment_run_id": str(assessment_run.id),
                    },
                    reuse_existing=True,
                )
                run = await self._repository.create_run(session, command)
                tasks = await self._repository.list_tasks(session, run.id)
                return PreparedProductionRun(
                    production_run_id=run.id,
                    task_count=len(tasks),
                    deadline_at=run.deadline_at,
                    required_outputs=tuple(
                        (task.artifact_key, task.output_profile)
                        for task in tasks
                    ),
                    task_ids=tuple(task.id for task in tasks),
                )

    async def create_rebuild_run(
        self,
        *,
        event_id: uuid.UUID,
        revision_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
        requested_by: str,
    ) -> PreparedProductionRun:
        if not requested_by.strip():
            raise ValueError("requested_by must not be empty")
        async with self._session_factory() as session:
            async with session.begin():
                event = await session.get(EarthquakeEvent, event_id)
                revision = await session.get(EarthquakeRevision, revision_id)
                if event is None or revision is None:
                    raise LookupError("artifact rebuild inputs were not found")
                if revision.event_id != event.id:
                    raise ValueError("artifact rebuild revision does not belong to event")
                self._catalog.get(artifact_key, output_profile)
                now = datetime.now(UTC)
                parent = await self._repository.get_current_full_run(
                    session,
                    event_id=event.id,
                    revision_id=revision.id,
                )
                command = CreateProductionRunCommand(
                    assessment_run_id=(
                        parent.assessment_run_id if parent is not None else None
                    ),
                    event_id=event.id,
                    revision_id=revision.id,
                    revision_no=revision.revision_no,
                    production_mode=_production_mode(revision.revision_kind),
                    launch_mode="standalone",
                    deadline_basis_at=now,
                    deadline_at=now + timedelta(seconds=300),
                    deadline_kind="rebuild_deadline",
                    catalog_version=self._catalog.catalog_version,
                    generation_scope=(
                        f"artifact:{artifact_key}:{output_profile}"
                    ),
                    required_outputs=((artifact_key, output_profile),),
                    rebuild_parent_run_id=parent.id if parent is not None else None,
                    snapshot={
                        "launch": "standalone",
                        "requested_by": requested_by,
                        "artifact_key": artifact_key,
                        "output_profile": output_profile,
                    },
                    reuse_existing=False,
                )
                run = await self._repository.create_run(session, command)
                tasks = await self._repository.list_tasks(session, run.id)
                return PreparedProductionRun(
                    production_run_id=run.id,
                    task_count=len(tasks),
                    deadline_at=run.deadline_at,
                    required_outputs=tuple(
                        (task.artifact_key, task.output_profile)
                        for task in tasks
                    ),
                    task_ids=tuple(task.id for task in tasks),
                )

    async def prepare_task(
        self,
        production_task_id: uuid.UUID,
    ) -> ArtifactRenderInput:
        async with self._session_factory() as session:
            async with session.begin():
                production_run_id = await session.scalar(
                    select(ProductionTask.production_run_id).where(
                        ProductionTask.id == production_task_id
                    )
                )
                if production_run_id is None:
                    raise LookupError("artifact production task not found")
                await self._repository.prepare_dependencies(
                    session,
                    production_run_id,
                )
                run = await session.get(
                    ProductionRun,
                    production_run_id,
                    with_for_update=True,
                )
                if run is None:
                    raise LookupError("artifact production run not found")
                if not run.is_current or run.superseded_at is not None:
                    raise ValueError("superseded production run cannot prepare tasks")
                if run.context_fingerprint is None:
                    raise ValueError(
                        "production input context must be frozen before task preparation"
                    )

                task = await session.get(
                    ProductionTask,
                    production_task_id,
                    with_for_update=True,
                )
                if task is None:
                    raise LookupError("artifact production task not found")
                bindings = (
                    await session.scalars(
                        select(ArtifactTaskDependencyBinding)
                        .where(
                            ArtifactTaskDependencyBinding.production_task_id
                            == task.id
                        )
                        .order_by(
                            ArtifactTaskDependencyBinding.dependency_kind,
                            ArtifactTaskDependencyBinding.dependency_key,
                            ArtifactTaskDependencyBinding.dependency_output_profile,
                        )
                    )
                ).all()
                hard_identities = {
                    (
                        item["kind"],
                        item["key"],
                        item.get("output_profile"),
                    )
                    for item in task.depends_on
                }
                resolved_hard = {
                    (
                        item.dependency_kind,
                        item.dependency_key,
                        item.dependency_output_profile,
                    )
                    for item in bindings
                    if item.resolution_status in {"bound", "degraded"}
                    and not item.is_optional
                }
                if not hard_identities <= resolved_hard:
                    raise ValueError("hard task dependencies are not ready")

                fingerprint = _input_fingerprint(
                    context_fingerprint=run.context_fingerprint,
                    artifact_key=task.artifact_key,
                    output_profile=task.output_profile,
                    bindings=bindings,
                )
                if task.input_fingerprint is None:
                    await self._repository.freeze_task_fingerprint(
                        session,
                        task.id,
                        fingerprint,
                    )
                elif task.input_fingerprint != fingerprint:
                    raise ValueError("task input fingerprint changed")

                return ArtifactRenderInput(
                    production_run_id=run.id,
                    production_task_id=task.id,
                    artifact_key=task.artifact_key,
                    output_profile=task.output_profile,
                    context_fingerprint=run.context_fingerprint,
                    input_fingerprint=task.input_fingerprint,
                    deadline_at=task.deadline_at,
                    dependency_bindings=tuple(bindings),
                )

    async def commit_task_result(
        self,
        task_id: uuid.UUID,
        result: ArtifactGenerationResult,
    ) -> GeneratedArtifact:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.complete_task(
                    session,
                    task_id,
                    result,
                    result.task_status,
                )


def _production_mode(revision_kind: str) -> str:
    mapping = {
        "formal": "live",
        "correction": "live",
        "manual": "manual",
        "test": "test",
        "drill": "drill",
        "replay": "replay",
    }
    try:
        return mapping[revision_kind]
    except KeyError as error:
        raise ValueError(
            f"unsupported artifact production revision kind: {revision_kind}"
        ) from error


def _input_fingerprint(
    *,
    context_fingerprint: str,
    artifact_key: str,
    output_profile: str,
    bindings: tuple[ArtifactTaskDependencyBinding, ...],
) -> str:
    payload: dict[str, Any] = {
        "context_fingerprint": context_fingerprint,
        "artifact_key": artifact_key,
        "output_profile": output_profile,
        "dependencies": [
            {
                "dependency_kind": binding.dependency_kind,
                "dependency_key": binding.dependency_key,
                "dependency_output_profile": binding.dependency_output_profile,
                "bound_entity_id": (
                    str(binding.bound_entity_id)
                    if binding.bound_entity_id is not None
                    else None
                ),
                "bound_version": binding.bound_version,
                "bound_checksum": binding.bound_checksum,
                "resolution_status": binding.resolution_status,
            }
            for binding in sorted(
                bindings,
                key=lambda item: (
                    item.dependency_kind,
                    item.dependency_key,
                    item.dependency_output_profile or "",
                ),
            )
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
