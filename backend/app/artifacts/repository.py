from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import (
    ArtifactCatalog,
    DependencyKind,
    DependencySpec,
    ResolutionStatus,
)
from app.artifacts.models import (
    ArtifactPublication,
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionInputSnapshotItem,
    ProductionRun,
    ProductionTask,
)
from app.assessment.models import AssessmentRun
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision
from app.intensity.models import IntensityFieldProduct
from app.loss.models import LossProduct


@dataclass(frozen=True, slots=True)
class ArtifactQuality:
    grade: str | None = None
    needs_review: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactGenerationResult:
    file_name: str
    format: str
    storage_path: str
    checksum: str
    size_bytes: int
    quality: ArtifactQuality = field(default_factory=ArtifactQuality)
    marker: Mapping[str, Any] | None = None
    width: int | None = None
    height: int | None = None
    page_count: int | None = None
    template_snapshot: Mapping[str, Any] | None = None
    data_snapshot: Mapping[str, Any] | None = None
    render_manifest: Mapping[str, Any] | None = None
    generated_at: datetime | None = None
    publication_mode: str | None = None
    task_status: str = "succeeded"

    def __post_init__(self) -> None:
        if not self.file_name.strip():
            raise ValueError("artifact file_name must not be empty")
        if not self.format.strip():
            raise ValueError("artifact format must not be empty")
        if not self.storage_path.strip():
            raise ValueError("artifact storage_path must not be empty")
        if len(self.checksum) != 64:
            raise ValueError("artifact checksum must contain 64 hexadecimal characters")
        try:
            int(self.checksum, 16)
        except ValueError as error:
            raise ValueError(
                "artifact checksum must contain 64 hexadecimal characters"
            ) from error
        if self.size_bytes < 0:
            raise ValueError("artifact size_bytes must not be negative")
        if self.width is not None and self.width <= 0:
            raise ValueError("artifact width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("artifact height must be positive")
        if self.page_count is not None and self.page_count <= 0:
            raise ValueError("artifact page_count must be positive")
        if self.generated_at is not None:
            _normalize_utc(self.generated_at, "generated_at")
        if self.publication_mode not in {
            None,
            "automatic",
            "rebuild",
            "superadmin_override",
        }:
            raise ValueError("invalid artifact publication mode")
        if self.task_status not in {"succeeded", "degraded"}:
            raise ValueError("artifact task_status must be succeeded or degraded")


@dataclass(frozen=True, slots=True)
class ProductionDependencyBinding:
    dependency_kind: str
    dependency_key: str
    dependency_output_profile: str | None
    is_optional: bool
    bound_entity_id: uuid.UUID | None
    bound_version: str | None
    bound_checksum: str | None
    resolution_status: str
    resolution_detail: Mapping[str, Any] | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            kind = DependencyKind(self.dependency_kind)
        except ValueError as error:
            raise ValueError("invalid dependency kind") from error
        if kind == DependencyKind.ASSESSMENT_PRODUCT:
            if self.dependency_output_profile is not None:
                raise ValueError(
                    "assessment product dependencies cannot have an output profile"
                )
        elif not self.dependency_output_profile:
            raise ValueError("artifact dependencies require an output profile")

        try:
            status = ResolutionStatus(self.resolution_status)
        except ValueError as error:
            raise ValueError("invalid dependency resolution status") from error
        bound_values = (
            self.bound_entity_id,
            self.bound_version,
            self.bound_checksum,
        )
        if any(value is None for value in bound_values) and any(
            value is not None for value in bound_values
        ):
            raise ValueError("dependency binding fields must be all set or all null")
        if status in {ResolutionStatus.BOUND, ResolutionStatus.DEGRADED} and any(
            value is None for value in bound_values
        ):
            raise ValueError("bound dependency must include entity, version and checksum")
        if status == ResolutionStatus.OMITTED_AFTER_WAIT and not self.is_optional:
            raise ValueError("omitted_after_wait dependency must be optional")
        if status in {
            ResolutionStatus.FAILED,
            ResolutionStatus.TIMED_OUT,
            ResolutionStatus.CANCELED,
            ResolutionStatus.OMITTED_AFTER_WAIT,
        } and (
            not self.resolution_detail or self.resolved_at is None
        ):
            raise ValueError(
                "terminal dependency must include resolution detail and resolved_at"
            )


@dataclass(frozen=True, slots=True)
class CreateProductionRunCommand:
    assessment_run_id: uuid.UUID | None
    event_id: uuid.UUID
    revision_id: uuid.UUID
    revision_no: int
    production_mode: str
    launch_mode: str
    deadline_basis_at: datetime
    deadline_at: datetime
    deadline_kind: str
    catalog_version: str
    generation_scope: str
    required_outputs: tuple[tuple[str, str], ...]
    priority: int = 100
    generation_seq: int | None = None
    rebuild_parent_run_id: uuid.UUID | None = None
    snapshot: Mapping[str, Any] | None = None
    reuse_existing: bool = False
    artifact_result: ArtifactGenerationResult | None = None
    published_by: str | None = None
    forced: bool = False

    def __post_init__(self) -> None:
        if self.revision_no < 1:
            raise ValueError("revision_no must be positive")
        if self.production_mode not in {
            "live",
            "manual",
            "test",
            "drill",
            "replay",
        }:
            raise ValueError("invalid production mode")
        if self.launch_mode not in {"assessment_child", "standalone"}:
            raise ValueError("invalid launch mode")
        if self.deadline_kind not in {"event_deadline", "rebuild_deadline"}:
            raise ValueError("invalid deadline kind")
        if self.generation_seq is not None and self.generation_seq < 1:
            raise ValueError("generation_seq must be positive")
        _normalize_utc(self.deadline_basis_at, "deadline_basis_at")
        _normalize_utc(self.deadline_at, "deadline_at")
        if self.deadline_at < self.deadline_basis_at:
            raise ValueError("deadline_at must not precede deadline_basis_at")


@dataclass(frozen=True, slots=True)
class PreparedProductionRun:
    production_run_id: uuid.UUID
    task_count: int
    deadline_at: datetime
    required_outputs: tuple[tuple[str, str], ...]
    task_ids: tuple[uuid.UUID, ...] = ()

    @property
    def run_id(self) -> uuid.UUID:
        return self.production_run_id


@dataclass(frozen=True, slots=True)
class TaskCompletion:
    task_id: uuid.UUID
    task_status: str
    artifact_result: ArtifactGenerationResult

    def __post_init__(self) -> None:
        if self.task_status not in {"succeeded", "degraded"}:
            raise ValueError("completed task status must be succeeded or degraded")


class ArtifactProductionRepository:
    def __init__(self, catalog: ArtifactCatalog | None = None) -> None:
        self._catalog = catalog

    @property
    def catalog(self) -> ArtifactCatalog:
        if self._catalog is None:
            self._catalog = load_catalog(settings.artifact_catalog_path)
        return self._catalog

    async def create_run(
        self,
        session: AsyncSession,
        command: CreateProductionRunCommand,
    ) -> ProductionRun:
        event = await session.get(
            EarthquakeEvent,
            command.event_id,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("artifact production event not found")
        revision = await session.get(EarthquakeRevision, command.revision_id)
        if revision is None or revision.event_id != event.id:
            raise ValueError("artifact production revision does not belong to event")
        if revision.revision_no != command.revision_no:
            raise ValueError("artifact production revision number changed")

        assessment_run: AssessmentRun | None = None
        if command.assessment_run_id is not None:
            assessment_run = await session.get(
                AssessmentRun,
                command.assessment_run_id,
                with_for_update=True,
            )
            if assessment_run is None:
                raise LookupError("assessment run not found")
            if (
                assessment_run.event_id != event.id
                or assessment_run.revision_id != revision.id
            ):
                raise ValueError("assessment run does not match event revision")

        expected_outputs = self.catalog.scope_required_outputs(
            command.generation_scope,
            "a3v-professional",
        )
        required_outputs = _normalize_outputs(command.required_outputs)
        if required_outputs != expected_outputs:
            raise ValueError("required_outputs do not match generation_scope")
        if command.generation_scope == "full" and len(required_outputs) != 39:
            raise ValueError("full production runs require exactly 39 outputs")
        if (
            command.generation_scope != "full"
            and len(required_outputs) != 1
        ):
            raise ValueError("artifact rebuild runs require exactly one output")

        snapshot = _json_mapping(command.snapshot)
        current = await session.scalar(
            select(ProductionRun)
            .where(
                ProductionRun.event_id == event.id,
                ProductionRun.revision_id == revision.id,
                ProductionRun.generation_scope == command.generation_scope,
                ProductionRun.is_current.is_(True),
                ProductionRun.superseded_at.is_(None),
            )
            .with_for_update()
        )
        if current is not None and command.reuse_existing:
            if (
                current.assessment_run_id == command.assessment_run_id
                and current.deadline_basis_at == command.deadline_basis_at
                and current.deadline_at == command.deadline_at
                and current.catalog_version == command.catalog_version
                and current.required_outputs == _required_outputs_json(
                    required_outputs
                )
                and current.snapshot == snapshot
            ):
                return current

        generation_seq = command.generation_seq
        if generation_seq is None:
            if assessment_run is not None:
                max_generation = await session.scalar(
                    select(func.max(ProductionRun.generation_seq)).where(
                        ProductionRun.assessment_run_id == assessment_run.id
                    )
                )
            else:
                max_generation = await session.scalar(
                    select(func.max(ProductionRun.generation_seq)).where(
                        ProductionRun.event_id == event.id,
                        ProductionRun.revision_id == revision.id,
                    )
                )
            generation_seq = int(max_generation or 0) + 1

        now = datetime.now(UTC)
        if current is not None:
            current.is_current = False
            current.superseded_at = now
            await session.flush()

        priority = min(command.priority, min(
            self.catalog.get(key, profile).priority
            for key, profile in required_outputs
        ))
        run = ProductionRun(
            assessment_run_id=command.assessment_run_id,
            event_id=event.id,
            revision_id=revision.id,
            revision_no=revision.revision_no,
            production_mode=command.production_mode,
            launch_mode=command.launch_mode,
            status="pending",
            priority=priority,
            deadline_basis_at=command.deadline_basis_at,
            deadline_at=command.deadline_at,
            deadline_kind=command.deadline_kind,
            catalog_version=command.catalog_version,
            generation_seq=generation_seq,
            generation_scope=command.generation_scope,
            required_outputs=_required_outputs_json(required_outputs),
            rebuild_parent_run_id=command.rebuild_parent_run_id,
            is_current=True,
            snapshot=snapshot or None,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        await session.flush()
        if current is not None:
            current.superseded_by_run_id = run.id
            await session.flush()

        optional_cutoff = run.deadline_at - timedelta(
            seconds=settings.artifact_optional_dependency_reserve_seconds
        )
        for sequence, (artifact_key, output_profile) in enumerate(
            required_outputs,
            start=1,
        ):
            definition = self.catalog.get(artifact_key, output_profile)
            session.add(
                ProductionTask(
                    production_run_id=run.id,
                    artifact_key=artifact_key,
                    output_profile=output_profile,
                    kind=definition.kind.value,
                    priority=definition.priority,
                    sequence=sequence,
                    status="pending",
                    depends_on=[
                        dependency.to_dict()
                        for dependency in definition.depends_on
                    ],
                    optional_depends_on=[
                        dependency.to_dict()
                        for dependency in definition.optional_depends_on
                    ],
                    optional_dependency_wait_cutoff_at=(
                        optional_cutoff
                        if definition.optional_depends_on
                        else None
                    ),
                    deadline_at=run.deadline_at,
                    attempt_count=0,
                    max_attempts=3,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        return run

    async def create_override_run(
        self,
        session: AsyncSession,
        command: CreateProductionRunCommand,
    ) -> tuple[ProductionRun, ProductionTask, GeneratedArtifact, ArtifactPublication]:
        if command.artifact_result is None:
            raise ValueError("override run command requires artifact_result")
        run = await self.create_run(session, command)
        tasks = await self.list_tasks(session, run.id)
        if len(tasks) != 1:
            raise ValueError("override run must contain exactly one task")
        task = tasks[0]
        task.kind = "superadmin_override"
        artifact_result = replace(
            command.artifact_result,
            publication_mode="superadmin_override",
        )
        fingerprint = artifact_result.render_manifest
        fingerprint_value = _fingerprint_from_manifest(fingerprint)
        await self.freeze_task_fingerprint(
            session,
            task.id,
            fingerprint_value,
        )
        await self.start_task(
            session,
            task.id,
            f"override:{run.id}:{task.artifact_key}:{fingerprint_value}",
        )
        artifact = await self.complete_task(
            session,
            task.id,
            artifact_result,
            artifact_result.task_status,
        )
        publication = await self.publish_artifact(
            session,
            artifact.id,
            published_by=command.published_by,
            forced=command.forced,
        )
        await self.finalize_run(
            session,
            run.id,
            observed_at=artifact.generated_at,
        )
        return run, task, artifact, publication

    async def create_input_snapshot(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        *,
        context_fingerprint: str,
        region_id: str,
        manifest: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]] = (),
    ) -> ProductionInputSnapshot:
        run_event_id = await session.scalar(
            select(ProductionRun.event_id).where(
                ProductionRun.id == production_run_id
            )
        )
        if run_event_id is None:
            raise LookupError("artifact production run not found")
        event = await session.get(
            EarthquakeEvent,
            run_event_id,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("artifact production event not found")
        assessment_run_id = await session.scalar(
            select(ProductionRun.assessment_run_id).where(
                ProductionRun.id == production_run_id
            )
        )
        if assessment_run_id is not None:
            await session.get(
                AssessmentRun,
                assessment_run_id,
                with_for_update=True,
            )
        run = await session.get(
            ProductionRun,
            production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        if len(context_fingerprint) != 64:
            raise ValueError("context_fingerprint must contain 64 hexadecimal characters")
        _validate_hex(context_fingerprint, "context_fingerprint")

        existing = await session.scalar(
            select(ProductionInputSnapshot)
            .where(ProductionInputSnapshot.production_run_id == run.id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.context_fingerprint == context_fingerprint
                and existing.region_id == region_id
                and existing.manifest == dict(manifest)
            ):
                return existing
            raise ValueError("production input snapshot is already frozen")

        snapshot = ProductionInputSnapshot(
            production_run_id=run.id,
            context_fingerprint=context_fingerprint,
            region_id=region_id,
            manifest=dict(manifest),
        )
        session.add(snapshot)
        await session.flush()
        for index, item in enumerate(items):
            snapshot_item = ProductionInputSnapshotItem(
                snapshot_id=snapshot.id,
                asset_key=str(item["asset_key"]),
                asset_version_id=_optional_uuid_value(
                    item.get("asset_version_id"),
                    f"items[{index}].asset_version_id",
                ),
                checksum=_optional_string_value(
                    item.get("checksum"),
                ),
                role=str(item["role"]),
                coverage=dict(item.get("coverage") or {}),
                selected_for_render=bool(
                    item.get("selected_for_render", False)
                ),
            )
            session.add(snapshot_item)
        await session.flush()
        run.input_snapshot_id = snapshot.id
        run.context_fingerprint = context_fingerprint
        await session.flush()
        return snapshot

    async def attach_input_snapshot(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        *,
        context_fingerprint: str,
        region_id: str,
        manifest: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]] = (),
    ) -> ProductionInputSnapshot:
        return await self.create_input_snapshot(
            session,
            production_run_id,
            context_fingerprint=context_fingerprint,
            region_id=region_id,
            manifest=manifest,
            items=items,
        )

    async def create_snapshot(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        *,
        context_fingerprint: str,
        region_id: str,
        manifest: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]] = (),
    ) -> ProductionInputSnapshot:
        return await self.create_input_snapshot(
            session,
            production_run_id,
            context_fingerprint=context_fingerprint,
            region_id=region_id,
            manifest=manifest,
            items=items,
        )

    async def prepare_dependencies(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
    ) -> tuple[ProductionTask, ...]:
        run_event_id = await session.scalar(
            select(ProductionRun.event_id).where(ProductionRun.id == production_run_id)
        )
        if run_event_id is None:
            raise LookupError("artifact production run not found")
        await session.get(
            EarthquakeEvent,
            run_event_id,
            with_for_update=True,
        )
        assessment_run_id = await session.scalar(
            select(ProductionRun.assessment_run_id).where(
                ProductionRun.id == production_run_id
            )
        )
        if assessment_run_id is not None:
            await session.get(
                AssessmentRun,
                assessment_run_id,
                with_for_update=True,
            )
        run = await session.get(
            ProductionRun,
            production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        tasks = (
            await session.scalars(
                select(ProductionTask)
                .where(ProductionTask.production_run_id == run.id)
                .order_by(ProductionTask.sequence, ProductionTask.id)
                .with_for_update()
            )
        ).all()
        prepared: list[ProductionTask] = []
        for task in tasks:
            now = datetime.now(UTC)
            existing = {
                (
                    binding.dependency_kind,
                    binding.dependency_key,
                    binding.dependency_output_profile,
                ): binding
                for binding in (
                    await session.scalars(
                        select(ArtifactTaskDependencyBinding).where(
                            ArtifactTaskDependencyBinding.production_task_id
                            == task.id
                        )
                    )
                ).all()
            }
            hard_satisfied = True
            optional_satisfied = True
            for dependency, is_optional in (
                *((item, False) for item in _task_dependencies(task.depends_on)),
                *(
                    (item, True)
                    for item in _task_dependencies(task.optional_depends_on)
                ),
            ):
                identity = (
                    dependency.kind.value,
                    dependency.key,
                    dependency.output_profile,
                )
                if identity in existing:
                    resolution_status = existing[identity].resolution_status
                    if not is_optional and resolution_status not in {
                        "bound",
                        "degraded",
                    }:
                        hard_satisfied = False
                    elif is_optional and resolution_status not in {
                        "bound",
                        "degraded",
                        "failed",
                        "timed_out",
                        "canceled",
                        "omitted_after_wait",
                    }:
                        optional_satisfied = False
                    continue
                resolved = await self._resolve_dependency(session, run, dependency)
                if resolved is None:
                    if not is_optional:
                        hard_satisfied = False
                    elif (
                        task.optional_dependency_wait_cutoff_at is not None
                        and now
                        >= task.optional_dependency_wait_cutoff_at
                    ):
                        await self.bind_dependency(
                            session,
                            task.id,
                            ProductionDependencyBinding(
                                dependency_kind=dependency.kind.value,
                                dependency_key=dependency.key,
                                dependency_output_profile=(
                                    dependency.output_profile
                                ),
                                is_optional=True,
                                bound_entity_id=None,
                                bound_version=None,
                                bound_checksum=None,
                                resolution_status="omitted_after_wait",
                                resolution_detail={
                                    "reason": "optional dependency not ready"
                                },
                                resolved_at=now,
                            ),
                        )
                    else:
                        optional_satisfied = False
                    continue
                await self.bind_dependency(
                    session,
                    task.id,
                    ProductionDependencyBinding(
                        dependency_kind=dependency.kind.value,
                        dependency_key=dependency.key,
                        dependency_output_profile=dependency.output_profile,
                        is_optional=is_optional,
                        bound_entity_id=resolved["entity_id"],
                        bound_version=resolved["version"],
                        bound_checksum=resolved["checksum"],
                        resolution_status=(
                            "degraded"
                            if resolved["degraded"]
                            else "bound"
                        ),
                        resolution_detail={"source": resolved["source"]},
                        resolved_at=now,
                    ),
                )
            if (
                hard_satisfied
                and optional_satisfied
                and task.status == "pending"
            ):
                task.status = "ready"
                task.updated_at = now
            prepared.append(task)
        await session.flush()
        return tuple(prepared)

    async def bind_dependency(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
        binding: ProductionDependencyBinding,
    ) -> ArtifactTaskDependencyBinding:
        task, run = await self._lock_task(session, production_task_id)
        declared = {
            (
                item.kind.value,
                item.key,
                item.output_profile,
            ): False
            for item in _task_dependencies(task.depends_on)
        }
        declared.update(
            {
                (
                    item.kind.value,
                    item.key,
                    item.output_profile,
                ): True
                for item in _task_dependencies(task.optional_depends_on)
            }
        )
        identity = (
            binding.dependency_kind,
            binding.dependency_key,
            binding.dependency_output_profile,
        )
        if identity not in declared:
            raise ValueError("dependency is not declared by production task")
        if declared[identity] != binding.is_optional:
            raise ValueError("dependency optionality changed")

        existing = await session.scalar(
            select(ArtifactTaskDependencyBinding)
            .where(
                ArtifactTaskDependencyBinding.production_task_id == task.id,
                ArtifactTaskDependencyBinding.dependency_kind
                == binding.dependency_kind,
                ArtifactTaskDependencyBinding.dependency_key
                == binding.dependency_key,
                ArtifactTaskDependencyBinding.dependency_output_profile.is_not_distinct_from(
                    binding.dependency_output_profile
                ),
            )
            .with_for_update()
        )
        if existing is not None:
            if _binding_matches(existing, binding):
                return existing
            raise ValueError("dependency binding is immutable")

        await self._validate_binding_reference(session, run, binding)
        row = ArtifactTaskDependencyBinding(
            production_task_id=task.id,
            dependency_kind=binding.dependency_kind,
            dependency_key=binding.dependency_key,
            dependency_output_profile=binding.dependency_output_profile,
            is_optional=binding.is_optional,
            bound_entity_id=binding.bound_entity_id,
            bound_version=binding.bound_version,
            bound_checksum=binding.bound_checksum,
            resolution_status=binding.resolution_status,
            resolution_detail=_json_mapping(binding.resolution_detail),
            resolved_at=binding.resolved_at or datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return row

    async def freeze_task_fingerprint(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
        fingerprint: str,
    ) -> ProductionTask:
        _validate_hex(fingerprint, "fingerprint")
        task, _run = await self._lock_task(session, production_task_id)
        if task.input_fingerprint is not None:
            if task.input_fingerprint != fingerprint:
                raise ValueError("task input fingerprint is already frozen")
            return task
        if task.status in {"succeeded", "degraded", "failed", "timed_out", "canceled"}:
            raise ValueError("terminal task cannot receive an input fingerprint")
        task.input_fingerprint = fingerprint
        task.updated_at = datetime.now(UTC)
        await session.flush()
        return task

    async def start_task(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
        activity_idempotency_key: str,
    ) -> ProductionTask:
        if not activity_idempotency_key:
            raise ValueError("activity idempotency key must not be empty")
        task, run = await self._lock_task(session, production_task_id)
        if run.status in {"completed", "partial", "failed", "canceled"}:
            raise ValueError("terminal production run cannot start tasks")
        if not run.is_current or run.superseded_at is not None:
            raise ValueError("superseded production run cannot start tasks")
        if task.input_fingerprint is None:
            raise ValueError("task input fingerprint must be frozen before start")

        current_result = dict(task.result or {})
        existing_key = current_result.get("activity_idempotency_key")
        if task.status in {"succeeded", "degraded"}:
            if existing_key != activity_idempotency_key:
                raise ValueError("completed task activity key changed")
            return task
        if task.status == "running":
            if existing_key != activity_idempotency_key:
                raise ValueError("running task activity key changed")
            return task
        if task.status in {"timed_out", "canceled"}:
            raise ValueError("terminal task cannot be restarted")
        if task.attempt_count >= task.max_attempts:
            raise ValueError("task retry budget exhausted")

        now = datetime.now(UTC)
        if now > task.deadline_at:
            raise ValueError("production task deadline has passed")
        task.status = "running"
        task.started_at = task.started_at or now
        task.attempt_count += 1
        task.result = {
            **current_result,
            "activity_idempotency_key": activity_idempotency_key,
        }
        task.updated_at = now
        if run.started_at is None:
            run.started_at = now
        run.status = "running"
        run.updated_at = now
        await session.flush()
        return task

    async def complete_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        artifact_result: ArtifactGenerationResult,
        task_status: str,
    ) -> GeneratedArtifact:
        if task_status not in {"succeeded", "degraded"}:
            raise ValueError("completed task status must be succeeded or degraded")
        if artifact_result.task_status != task_status:
            raise ValueError("artifact result task status does not match completion")
        task, run = await self._lock_task(session, task_id)
        if run.status in {"completed", "partial", "failed", "canceled"}:
            raise ValueError("terminal production run cannot commit artifacts")
        if task.status in {"succeeded", "degraded"} and task.final_artifact_id is not None:
            existing = await session.get(
                GeneratedArtifact,
                task.final_artifact_id,
                with_for_update=True,
            )
            if (
                existing is not None
                and _artifact_result_matches(existing, artifact_result)
                and existing.status
                == ("degraded" if task_status == "degraded" else "complete")
            ):
                return existing
            raise ValueError("completed task result is immutable")
        if task.status != "running":
            raise ValueError("task must be running before it can complete")
        if not run.is_current or run.superseded_at is not None:
            raise ValueError("superseded production run cannot commit artifacts")

        now = artifact_result.generated_at or datetime.now(UTC)
        now = _normalize_utc(now, "generated_at")
        if now > run.deadline_at or now > task.deadline_at:
            raise ValueError("artifact write occurs after production deadline")

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"artifact-version:{run.event_id}:{task.artifact_key}:"
                    f"{task.output_profile}"
                )
            },
        )
        max_version = await session.scalar(
            select(func.max(GeneratedArtifact.artifact_version)).where(
                GeneratedArtifact.event_id == run.event_id,
                GeneratedArtifact.artifact_key == task.artifact_key,
                GeneratedArtifact.output_profile == task.output_profile,
            )
        )
        artifact_version = int(max_version or 0) + 1

        if task.final_artifact_id is not None:
            previous = await session.get(
                GeneratedArtifact,
                task.final_artifact_id,
                with_for_update=True,
            )
            if previous is not None:
                previous.is_final = False
                await session.flush()

        publication_mode = artifact_result.publication_mode or (
            "rebuild"
            if run.launch_mode == "standalone"
            or run.deadline_kind == "rebuild_deadline"
            else "automatic"
        )
        artifact = GeneratedArtifact(
            production_run_id=run.id,
            production_task_id=task.id,
            event_id=run.event_id,
            revision_id=run.revision_id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            artifact_version=artifact_version,
            is_final=True,
            production_mode=run.production_mode,
            status="degraded" if task_status == "degraded" else "complete",
            quality_grade=artifact_result.quality.grade,
            needs_review=artifact_result.quality.needs_review,
            publication_mode=publication_mode,
            marker=_json_mapping(artifact_result.marker),
            file_name=artifact_result.file_name,
            format=artifact_result.format,
            storage_path=artifact_result.storage_path,
            checksum=artifact_result.checksum,
            size_bytes=artifact_result.size_bytes,
            width=artifact_result.width,
            height=artifact_result.height,
            page_count=artifact_result.page_count,
            template_snapshot=_json_mapping(artifact_result.template_snapshot),
            data_snapshot=_json_mapping(artifact_result.data_snapshot),
            render_manifest=_json_mapping(artifact_result.render_manifest),
            generated_at=now,
            created_at=now,
        )
        session.add(artifact)
        await session.flush()

        task.status = task_status
        task.completed_at = now
        task.output_checksum = artifact.checksum
        task.final_artifact_id = artifact.id
        task.result = {
            **dict(task.result or {}),
            "artifact_status": artifact.status,
            "quality_grade": artifact.quality_grade,
            "needs_review": artifact.needs_review,
            "degradation_reasons": list(
                artifact_result.quality.degradation_reasons
            ),
        }
        task.updated_at = now
        run.last_artifact_committed_at = _max_datetime(
            run.last_artifact_committed_at,
            now,
        )
        run.updated_at = now
        await session.flush()
        return artifact

    async def fail_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        error_category: str,
        error_summary: str,
    ) -> ProductionTask:
        if not error_category.strip() or not error_summary.strip():
            raise ValueError("task error category and summary must not be empty")
        task, run = await self._lock_task(session, task_id)
        if run.status in {"completed", "partial", "failed", "canceled"}:
            raise ValueError("terminal production run cannot fail tasks")
        if task.status in {
            "succeeded",
            "degraded",
            "failed",
            "timed_out",
            "canceled",
        }:
            raise ValueError("terminal task cannot be failed")
        now = datetime.now(UTC)
        task.status = "failed"
        task.completed_at = now
        task.last_error = error_summary[:2000]
        task.result = {
            **dict(task.result or {}),
            "error_category": error_category[:64],
            "error_summary": error_summary[:2000],
        }
        if now > task.deadline_at and task.deadline_exceeded_at is None:
            task.deadline_exceeded_at = now
        task.updated_at = now
        if now > run.deadline_at and run.deadline_exceeded_at is None:
            run.deadline_exceeded_at = now
        run.updated_at = now
        await session.flush()
        return task

    async def timeout_run(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        observed_at: datetime,
    ) -> ProductionRun:
        observed_at = _normalize_utc(observed_at, "observed_at")
        run = await self._lock_run(session, production_run_id)
        if run.status in {"completed", "partial", "failed", "canceled"}:
            return run
        tasks = (
            await session.scalars(
                select(ProductionTask)
                .where(
                    ProductionTask.production_run_id == run.id,
                    ProductionTask.status.in_(
                        ("pending", "ready", "running")
                    ),
                )
                .order_by(ProductionTask.sequence)
                .with_for_update()
            )
        ).all()
        for task in tasks:
            task.status = "timed_out"
            task.completed_at = observed_at
            if task.deadline_exceeded_at is None:
                task.deadline_exceeded_at = observed_at
            task.updated_at = observed_at
        run.updated_at = observed_at
        await session.flush()
        return await self.finalize_run(session, run.id, observed_at)

    async def finalize_run(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        observed_at: datetime,
    ) -> ProductionRun:
        observed_at = _normalize_utc(observed_at, "observed_at")
        run = await self._lock_run(session, production_run_id)
        if run.status in {"completed", "partial", "failed", "canceled"}:
            return run
        tasks = (
            await session.scalars(
                select(ProductionTask)
                .where(ProductionTask.production_run_id == run.id)
                .order_by(ProductionTask.sequence, ProductionTask.id)
                .with_for_update()
            )
        ).all()
        successful = [
            task
            for task in tasks
            if task.status in {"succeeded", "degraded"}
        ]
        unfinished = [
            task
            for task in tasks
            if task.status in {"pending", "ready", "running"}
        ]
        if unfinished and observed_at < run.deadline_at:
            raise ValueError(
                "cannot finalize production run with unfinished tasks before deadline"
            )
        if observed_at >= run.deadline_at:
            for task in unfinished:
                task.status = "timed_out"
                task.completed_at = observed_at
                if task.deadline_exceeded_at is None:
                    task.deadline_exceeded_at = observed_at
                task.updated_at = observed_at

        late_commit = any(
            task.completed_at is not None and task.completed_at > run.deadline_at
            for task in successful
        )
        timed_out = any(task.status == "timed_out" for task in tasks)
        if late_commit or timed_out:
            if run.deadline_exceeded_at is None:
                run.deadline_exceeded_at = observed_at

        if tasks and len(successful) == len(tasks) and not late_commit:
            run.status = "completed"
        elif successful:
            run.status = "partial"
        else:
            run.status = "failed"
        run.completed_at = observed_at
        run.final_input_fingerprint = _final_fingerprint(tasks)
        run.last_artifact_committed_at = _max_filtered_datetime(
            run.last_artifact_committed_at,
            [
                task.completed_at
                for task in successful
                if task.completed_at is not None
            ],
        )
        run.updated_at = observed_at
        await session.flush()
        return run

    async def publish_artifact(
        self,
        session: AsyncSession,
        artifact_id: uuid.UUID,
        published_by: str | None,
        forced: bool,
    ) -> ArtifactPublication:
        artifact_snapshot = await session.get(GeneratedArtifact, artifact_id)
        if artifact_snapshot is None:
            raise LookupError("generated artifact not found")
        event = await session.get(
            EarthquakeEvent,
            artifact_snapshot.event_id,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("artifact event not found")
        assessment_run_id = await session.scalar(
            select(ProductionRun.assessment_run_id).where(
                ProductionRun.id == artifact_snapshot.production_run_id
            )
        )
        if assessment_run_id is not None:
            await session.get(
                AssessmentRun,
                assessment_run_id,
                with_for_update=True,
            )
        run = await session.get(
            ProductionRun,
            artifact_snapshot.production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        task = await session.get(
            ProductionTask,
            artifact_snapshot.production_task_id,
            with_for_update=True,
        )
        if task is None:
            raise LookupError("artifact production task not found")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"artifact-publication:{artifact_snapshot.event_id}:"
                    f"{artifact_snapshot.artifact_key}:"
                    f"{artifact_snapshot.output_profile}:"
                    f"{artifact_snapshot.production_mode}"
                )
            },
        )
        artifact = await session.get(
            GeneratedArtifact,
            artifact_id,
            with_for_update=True,
        )
        if artifact is None:
            raise LookupError("generated artifact not found")
        if artifact.status == "failed":
            raise ValueError("failed artifact cannot be published")
        if not run.is_current or run.superseded_at is not None:
            raise ValueError("superseded production run cannot publish artifacts")
        if run.status in {"canceled", "failed"}:
            raise ValueError("terminal production run cannot publish artifacts")
        if event.current_revision_id != run.revision_id:
            raise ValueError("artifact revision is no longer current")
        revision = await session.get(EarthquakeRevision, run.revision_id)
        if revision is None or not revision.is_current:
            raise ValueError("artifact revision is superseded")

        current_candidate = await session.scalar(
            select(ArtifactPublication)
            .where(
                ArtifactPublication.event_id == artifact.event_id,
                ArtifactPublication.artifact_key == artifact.artifact_key,
                ArtifactPublication.output_profile == artifact.output_profile,
                ArtifactPublication.production_mode == artifact.production_mode,
                ArtifactPublication.superseded_at.is_(None),
            )
        )
        old_artifact: GeneratedArtifact | None = None
        if current_candidate is not None:
            old_artifact = await session.get(
                GeneratedArtifact,
                current_candidate.artifact_id,
                with_for_update=True,
            )
            if old_artifact is None:
                raise LookupError("current publication artifact not found")
            current = await session.scalar(
                select(ArtifactPublication)
                .where(
                    ArtifactPublication.id == current_candidate.id,
                    ArtifactPublication.superseded_at.is_(None),
                )
                .with_for_update()
            )
            if (
                current is None
                or current.artifact_id != current_candidate.artifact_id
            ):
                raise RuntimeError("publication state changed concurrently")
        else:
            current = await session.scalar(
                select(ArtifactPublication)
                .where(
                    ArtifactPublication.event_id == artifact.event_id,
                    ArtifactPublication.artifact_key == artifact.artifact_key,
                    ArtifactPublication.output_profile == artifact.output_profile,
                    ArtifactPublication.production_mode
                    == artifact.production_mode,
                    ArtifactPublication.superseded_at.is_(None),
                )
                .with_for_update()
            )
            if current is not None:
                raise RuntimeError("publication state changed concurrently")
        if current is not None and current.artifact_id == artifact.id:
            return current
        if current is not None and not forced:
            if artifact.production_run_id != current.production_run_id:
                if run.generation_seq < current.generation_seq:
                    raise ValueError("lower generation sequence cannot replace publication")
                if run.generation_seq == current.generation_seq:
                    raise ValueError(
                        "same generation sequence cannot replace publication"
                    )

        now = datetime.now(UTC)
        if current is not None:
            current.superseded_at = now
            if old_artifact is not None:
                if old_artifact.production_task_id == artifact.production_task_id:
                    old_artifact.is_final = False
                old_artifact.superseded_by_id = artifact.id
            await session.flush()

        publication = ArtifactPublication(
            event_id=artifact.event_id,
            revision_id=artifact.revision_id,
            revision_no=run.revision_no,
            production_mode=artifact.production_mode,
            artifact_key=artifact.artifact_key,
            output_profile=artifact.output_profile,
            artifact_id=artifact.id,
            production_run_id=run.id,
            generation_seq=run.generation_seq,
            published_by=published_by,
            published_at=now,
            is_forced=forced,
        )
        session.add(publication)
        artifact.is_final = True
        artifact.published_at = now
        await session.flush()
        return publication

    async def supersede_runs_for_revision(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        new_revision_id: uuid.UUID,
        new_revision_no: int,
    ) -> tuple[uuid.UUID, ...]:
        event = await session.get(EarthquakeEvent, event_id, with_for_update=True)
        if event is None:
            raise LookupError("artifact event not found")
        new_revision = await session.get(EarthquakeRevision, new_revision_id)
        if (
            new_revision is None
            or new_revision.event_id != event.id
            or new_revision.revision_no != new_revision_no
        ):
            raise ValueError("new revision does not match correction request")

        candidate_runs = (
            await session.execute(
                select(ProductionRun.id, ProductionRun.assessment_run_id)
                .where(
                    ProductionRun.event_id == event.id,
                    ProductionRun.revision_id != new_revision.id,
                    ProductionRun.superseded_at.is_(None),
                )
                .order_by(ProductionRun.created_at, ProductionRun.id)
            )
        ).all()
        await self._lock_assessment_runs(
            session,
            (
                assessment_run_id
                for _, assessment_run_id in candidate_runs
                if assessment_run_id is not None
            ),
        )
        runs = (
            await session.scalars(
                select(ProductionRun)
                .where(
                    ProductionRun.event_id == event.id,
                    ProductionRun.revision_id != new_revision.id,
                    ProductionRun.superseded_at.is_(None),
                )
                .order_by(ProductionRun.created_at, ProductionRun.id)
                .with_for_update()
            )
        ).all()
        now = datetime.now(UTC)
        canceled_ids: list[uuid.UUID] = []
        for run in runs:
            run.is_current = False
            run.superseded_at = now
            run.cancel_requested_at = now
            run.cancel_reason = "revision_superseded"
            run.priority = 0
            if run.status not in {"completed", "partial", "failed"}:
                run.status = "canceled"
                run.completed_at = now
            run.updated_at = now
            canceled_ids.append(run.id)
            await session.flush()

        await self._cancel_unfinished_run_tasks(session, canceled_ids, now)
        old_revision_ids = set(
            (
                await session.scalars(
                    select(EarthquakeRevision.id).where(
                        EarthquakeRevision.event_id == event.id,
                        EarthquakeRevision.id != new_revision.id,
                    )
                )
            ).all()
        )
        if old_revision_ids:
            publications = (
                await session.scalars(
                    select(ArtifactPublication)
                    .where(
                        ArtifactPublication.event_id == event.id,
                        ArtifactPublication.revision_id.in_(old_revision_ids),
                        ArtifactPublication.superseded_at.is_(None),
                    )
                    .order_by(ArtifactPublication.id)
                    .with_for_update()
                )
            ).all()
            for publication in publications:
                publication.superseded_at = now
        await session.flush()
        return tuple(canceled_ids)

    async def cancel_non_live_runs_for_real_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        event = await session.get(EarthquakeEvent, event_id, with_for_update=True)
        if event is None:
            raise LookupError("artifact event not found")
        candidate_runs = (
            await session.execute(
                select(ProductionRun.id, ProductionRun.assessment_run_id)
                .where(
                    ProductionRun.event_id == event.id,
                    ProductionRun.production_mode != "live",
                )
                .order_by(ProductionRun.created_at, ProductionRun.id)
            )
        ).all()
        await self._lock_assessment_runs(
            session,
            (
                assessment_run_id
                for _, assessment_run_id in candidate_runs
                if assessment_run_id is not None
            ),
        )
        runs = (
            await session.scalars(
                select(ProductionRun)
                .where(
                    ProductionRun.event_id == event.id,
                    ProductionRun.production_mode != "live",
                )
                .order_by(ProductionRun.created_at, ProductionRun.id)
                .with_for_update()
            )
        ).all()
        now = datetime.now(UTC)
        canceled_ids: list[uuid.UUID] = []
        for run in runs:
            run.is_current = False
            run.cancel_requested_at = now
            run.cancel_reason = "real_event_priority"
            run.priority = 0
            if run.status not in {"completed", "partial", "failed"}:
                run.status = "canceled"
                run.completed_at = now
            run.updated_at = now
            canceled_ids.append(run.id)
        await self._cancel_unfinished_run_tasks(session, canceled_ids, now)
        await session.flush()
        return tuple(canceled_ids)

    async def _lock_assessment_runs(
        self,
        session: AsyncSession,
        assessment_run_ids: Iterable[uuid.UUID],
    ) -> None:
        ids = sorted({item for item in assessment_run_ids}, key=str)
        if not ids:
            return
        (
            await session.scalars(
                select(AssessmentRun)
                .where(AssessmentRun.id.in_(ids))
                .order_by(AssessmentRun.id)
                .with_for_update()
            )
        ).all()

    async def _cancel_unfinished_run_tasks(
        self,
        session: AsyncSession,
        run_ids: Sequence[uuid.UUID],
        observed_at: datetime,
    ) -> None:
        if not run_ids:
            return
        tasks = (
            await session.scalars(
                select(ProductionTask)
                .where(
                    ProductionTask.production_run_id.in_(run_ids),
                    ProductionTask.status.in_(("pending", "ready", "running")),
                )
                .order_by(
                    ProductionTask.production_run_id,
                    ProductionTask.sequence,
                    ProductionTask.id,
                )
                .with_for_update()
            )
        ).all()
        for task in tasks:
            task.status = "canceled"
            task.completed_at = observed_at
            task.updated_at = observed_at

    async def get_current_full_run(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> ProductionRun | None:
        return await session.scalar(
            select(ProductionRun).where(
                ProductionRun.event_id == event_id,
                ProductionRun.revision_id == revision_id,
                ProductionRun.generation_scope == "full",
                ProductionRun.is_current.is_(True),
                ProductionRun.superseded_at.is_(None),
            )
        )

    async def get_publication(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
        production_mode: str,
    ) -> ArtifactPublication | None:
        return await session.scalar(
            select(ArtifactPublication).where(
                ArtifactPublication.event_id == event_id,
                ArtifactPublication.artifact_key == artifact_key,
                ArtifactPublication.output_profile == output_profile,
                ArtifactPublication.production_mode == production_mode,
                ArtifactPublication.superseded_at.is_(None),
            )
        )

    async def list_tasks(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
    ) -> list[ProductionTask]:
        tasks = (
            await session.scalars(
                select(ProductionTask)
                .where(ProductionTask.production_run_id == production_run_id)
                .order_by(ProductionTask.sequence, ProductionTask.id)
            )
        ).all()
        return list(tasks)

    async def mark_task_succeeded_for_test(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        observed_at: datetime | None = None,
    ) -> ProductionTask:
        task, run = await self._lock_task(session, task_id)
        now = observed_at or datetime.now(UTC)
        now = _normalize_utc(now, "observed_at")
        task.status = "succeeded"
        task.completed_at = now
        task.updated_at = now
        run.last_artifact_committed_at = _max_datetime(
            run.last_artifact_committed_at,
            now,
        )
        if now > run.deadline_at and run.deadline_exceeded_at is None:
            run.deadline_exceeded_at = now
        await session.flush()
        return task

    async def mark_task_failed_for_test(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        observed_at: datetime | None = None,
    ) -> ProductionTask:
        task, run = await self._lock_task(session, task_id)
        now = observed_at or datetime.now(UTC)
        now = _normalize_utc(now, "observed_at")
        task.status = "failed"
        task.completed_at = now
        if now > task.deadline_at:
            task.deadline_exceeded_at = now
        task.updated_at = now
        if now > run.deadline_at and run.deadline_exceeded_at is None:
            run.deadline_exceeded_at = now
        await session.flush()
        return task

    async def _lock_run(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
    ) -> ProductionRun:
        event_id = await session.scalar(
            select(ProductionRun.event_id).where(
                ProductionRun.id == production_run_id
            )
        )
        if event_id is None:
            raise LookupError("artifact production run not found")
        await session.get(EarthquakeEvent, event_id, with_for_update=True)
        assessment_run_id = await session.scalar(
            select(ProductionRun.assessment_run_id).where(
                ProductionRun.id == production_run_id
            )
        )
        if assessment_run_id is not None:
            await session.get(
                AssessmentRun,
                assessment_run_id,
                with_for_update=True,
            )
        run = await session.get(
            ProductionRun,
            production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        return run

    async def _lock_task(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
    ) -> tuple[ProductionTask, ProductionRun]:
        context = (
            await session.execute(
                select(
                    ProductionTask.production_run_id,
                    ProductionRun.event_id,
                    ProductionRun.assessment_run_id,
                )
                .join(
                    ProductionRun,
                    ProductionRun.id == ProductionTask.production_run_id,
                )
                .where(ProductionTask.id == production_task_id)
            )
        ).one_or_none()
        if context is None:
            raise LookupError("artifact production task not found")
        await session.get(
            EarthquakeEvent,
            context.event_id,
            with_for_update=True,
        )
        if context.assessment_run_id is not None:
            await session.get(
                AssessmentRun,
                context.assessment_run_id,
                with_for_update=True,
            )
        run = await session.get(
            ProductionRun,
            context.production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        task = await session.get(
            ProductionTask,
            production_task_id,
            with_for_update=True,
        )
        if task is None:
            raise LookupError("artifact production task not found")
        return task, run

    async def _resolve_dependency(
        self,
        session: AsyncSession,
        run: ProductionRun,
        dependency: DependencySpec,
    ) -> dict[str, Any] | None:
        if dependency.kind == DependencyKind.ASSESSMENT_PRODUCT:
            if run.assessment_run_id is None:
                return None
            product_types = {
                "intensity.fusion": ("intensity", "fusion"),
                "loss.buildings": ("loss", "building_damage"),
                "loss.population": ("loss", "population_impact"),
                "loss.casualties": ("loss", "casualties"),
                "loss.economic": ("loss", "economic_loss"),
                "loss.resources": ("loss", "resource_demand"),
                "loss.validate": ("loss", "validation"),
            }
            mapping = product_types.get(dependency.key)
            if mapping is None:
                raise ValueError(
                    f"unsupported assessment product dependency: {dependency.key}"
                )
            family, product_type = mapping
            if family == "intensity":
                product = await session.scalar(
                    select(IntensityFieldProduct).where(
                        IntensityFieldProduct.run_id == run.assessment_run_id,
                        IntensityFieldProduct.product_type == product_type,
                    )
                )
                if product is None or product.output_checksum is None:
                    return None
                return {
                    "entity_id": product.id,
                    "version": product.algorithm_version,
                    "checksum": product.output_checksum,
                    "degraded": product.quality_grade not in {None, "A"},
                    "source": "assessment_product",
                }
            product = await session.scalar(
                select(LossProduct).where(
                    LossProduct.run_id == run.assessment_run_id,
                    LossProduct.product_type == product_type,
                )
            )
            if product is None or product.status not in {"complete", "partial"}:
                return None
            return {
                "entity_id": product.id,
                "version": product.algorithm_version,
                "checksum": product.output_checksum,
                "degraded": product.status == "partial" or product.needs_review,
                "source": "assessment_product",
            }

        if not dependency.output_profile:
            return None
        artifact = await session.scalar(
            select(GeneratedArtifact)
            .where(
                GeneratedArtifact.event_id == run.event_id,
                GeneratedArtifact.revision_id == run.revision_id,
                GeneratedArtifact.artifact_key == dependency.key,
                GeneratedArtifact.output_profile == dependency.output_profile,
                GeneratedArtifact.is_final.is_(True),
            )
            .order_by(
                GeneratedArtifact.artifact_version.desc(),
                GeneratedArtifact.created_at.desc(),
            )
        )
        if artifact is None:
            return None
        return {
            "entity_id": artifact.id,
            "version": str(artifact.artifact_version),
            "checksum": artifact.checksum,
            "degraded": artifact.status == "degraded",
            "source": "generated_artifact",
        }

    async def _validate_binding_reference(
        self,
        session: AsyncSession,
        run: ProductionRun,
        binding: ProductionDependencyBinding,
    ) -> None:
        if binding.bound_entity_id is None:
            return
        if binding.dependency_kind == DependencyKind.ASSESSMENT_PRODUCT.value:
            resolved = await self._resolve_dependency(
                session,
                run,
                DependencySpec(
                    kind=DependencyKind.ASSESSMENT_PRODUCT,
                    key=binding.dependency_key,
                ),
            )
        else:
            resolved = await self._resolve_dependency(
                session,
                run,
                DependencySpec(
                    kind=DependencyKind.ARTIFACT,
                    key=binding.dependency_key,
                    output_profile=binding.dependency_output_profile,
                ),
            )
        if resolved is None or (
            resolved["entity_id"],
            resolved["version"],
            resolved["checksum"],
        ) != (
            binding.bound_entity_id,
            binding.bound_version,
            binding.bound_checksum,
        ):
            raise ValueError("dependency binding does not reference the current input")


def _normalize_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _optional_uuid_value(
    value: object,
    field_name: str,
) -> uuid.UUID | None:
    if value is None:
        return None
    return _coerce_uuid(value, field_name)


def _optional_string_value(
    value: object,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _validate_hex(value: str, field_name: str) -> None:
    if len(value) != 64:
        raise ValueError(f"{field_name} must contain 64 hexadecimal characters")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError(
            f"{field_name} must contain 64 hexadecimal characters"
        ) from error


def _normalize_outputs(
    outputs: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for artifact_key, output_profile in outputs:
        if not artifact_key or not output_profile:
            raise ValueError("required outputs must contain artifact key and profile")
        normalized.append((artifact_key, output_profile))
    if len(normalized) != len(set(normalized)):
        raise ValueError("required outputs cannot contain duplicates")
    return tuple(normalized)


def _required_outputs_json(
    outputs: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    return [
        {"artifact_key": artifact_key, "output_profile": output_profile}
        for artifact_key, output_profile in outputs
    ]


def _task_dependencies(value: object) -> tuple[DependencySpec, ...]:
    if not isinstance(value, list):
        raise ValueError("task dependencies must be an array")
    return tuple(DependencySpec.from_dict(item) for item in value)


def _json_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))


def _binding_matches(
    row: ArtifactTaskDependencyBinding,
    binding: ProductionDependencyBinding,
) -> bool:
    return (
        row.is_optional == binding.is_optional
        and row.bound_entity_id == binding.bound_entity_id
        and row.bound_version == binding.bound_version
        and row.bound_checksum == binding.bound_checksum
        and row.resolution_status == binding.resolution_status
        and row.resolution_detail == (
            dict(binding.resolution_detail)
            if binding.resolution_detail is not None
            else None
        )
    )


def _artifact_result_matches(
    artifact: GeneratedArtifact,
    result: ArtifactGenerationResult,
) -> bool:
    return (
        artifact.file_name == result.file_name
        and artifact.format == result.format
        and artifact.storage_path == result.storage_path
        and artifact.checksum == result.checksum
        and artifact.size_bytes == result.size_bytes
        and artifact.quality_grade == result.quality.grade
        and artifact.needs_review == result.quality.needs_review
    )


def _max_datetime(
    current: datetime | None,
    candidate: datetime,
) -> datetime:
    if current is None or candidate > current:
        return candidate
    return current


def _max_filtered_datetime(
    current: datetime | None,
    candidates: Sequence[datetime],
) -> datetime | None:
    for candidate in candidates:
        current = _max_datetime(current, candidate)
    return current


def _final_fingerprint(tasks: Sequence[ProductionTask]) -> str:
    payload = [
        {
            "artifact_key": task.artifact_key,
            "output_profile": task.output_profile,
            "input_fingerprint": task.input_fingerprint,
            "status": task.status,
        }
        for task in sorted(
            tasks,
            key=lambda item: (item.artifact_key, item.output_profile),
        )
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fingerprint_from_manifest(value: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()
