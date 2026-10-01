from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import ArtifactCatalog, ArtifactDefinition
from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
    CreateProductionRunCommand,
    PreparedProductionRun,
)
from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.artifacts.validation import ArtifactValidator, ValidationResult
from app.assessment.models import AssessmentRun
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with a different request."""


class ArtifactOverrideValidationError(ValueError):
    def __init__(
        self,
        *,
        error_category: str,
        summary: str,
    ) -> None:
        super().__init__(summary)
        self.error_category = error_category
        self.summary = summary


class ArtifactOverrideLeaseError(RuntimeError):
    """Raised when an override lease generation can no longer commit."""


@dataclass(frozen=True, slots=True)
class ArtifactOverrideResponse:
    artifact_id: uuid.UUID
    production_run_id: uuid.UUID
    production_task_id: uuid.UUID
    artifact_publication_id: uuid.UUID
    status: str
    generation_seq: int
    file_name: str
    checksum: str
    size_bytes: int
    generated_at: datetime
    superseded_artifact_id: uuid.UUID | None = None

    def to_body(self) -> dict[str, Any]:
        return {
            "artifact_id": str(self.artifact_id),
            "production_run_id": str(self.production_run_id),
            "production_task_id": str(self.production_task_id),
            "artifact_publication_id": str(self.artifact_publication_id),
            "status": self.status,
            "generation_seq": self.generation_seq,
            "file_name": self.file_name,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "generated_at": self.generated_at.isoformat(),
            "superseded_artifact_id": (
                str(self.superseded_artifact_id)
                if self.superseded_artifact_id is not None
                else None
            ),
        }

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "ArtifactOverrideResponse":
        generated_at = body.get("generated_at")
        return cls(
            artifact_id=uuid.UUID(str(body["artifact_id"])),
            production_run_id=uuid.UUID(str(body["production_run_id"])),
            production_task_id=uuid.UUID(str(body["production_task_id"])),
            artifact_publication_id=uuid.UUID(
                str(body["artifact_publication_id"])
            ),
            status=str(body["status"]),
            generation_seq=int(body["generation_seq"]),
            file_name=str(body["file_name"]),
            checksum=str(body["checksum"]),
            size_bytes=int(body["size_bytes"]),
            generated_at=(
                datetime.fromisoformat(str(generated_at))
                if generated_at is not None
                else datetime.now(UTC)
            ),
            superseded_artifact_id=(
                uuid.UUID(str(body["superseded_artifact_id"]))
                if body.get("superseded_artifact_id") is not None
                else None
            ),
        )


def sha256_json(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
                    deadline_basis_at=run.deadline_basis_at,
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
        reason: str | None = None,
    ) -> PreparedProductionRun:
        if not requested_by.strip():
            raise ValueError("requested_by must not be empty")
        if reason is not None and not reason.strip():
            raise ValueError("reason must not be empty")
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
                production_mode = _production_mode(revision.revision_kind)
                old_publication = await self._repository.get_publication(
                    session,
                    event_id=event.id,
                    artifact_key=artifact_key,
                    output_profile=output_profile,
                    production_mode=production_mode,
                )
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
                    production_mode=production_mode,
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
                        "reason": reason,
                    },
                    reuse_existing=False,
                )
                run = await self._repository.create_run(session, command)
                run.snapshot = {
                    **dict(run.snapshot or {}),
                    "operator": requested_by,
                    "rebuild_requested_at": now.isoformat(),
                    "artifact_key": artifact_key,
                    "output_profile": output_profile,
                    "old_current_artifact_id": (
                        str(old_publication.artifact_id)
                        if old_publication is not None
                        else None
                    ),
                    "new_production_run_id": str(run.id),
                    "reason": reason,
                }
                await session.flush()
                tasks = await self._repository.list_tasks(session, run.id)
                return PreparedProductionRun(
                    production_run_id=run.id,
                    task_count=len(tasks),
                    deadline_basis_at=run.deadline_basis_at,
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
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {
                        "lock_key": f"artifact-task-write:{production_run_id}",
                    },
                )
                initial_status = await session.scalar(
                    select(ProductionTask.status).where(
                        ProductionTask.id == production_task_id
                    )
                )
                if initial_status is None:
                    raise LookupError("artifact production task not found")
                if initial_status == "pending":
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
                task = await session.get(
                    ProductionTask,
                    production_task_id,
                    with_for_update=True,
                )
                if task is None:
                    raise LookupError("artifact production task not found")
                if not run.is_current or run.superseded_at is not None:
                    raise ValueError("superseded production run cannot prepare tasks")
                if run.context_fingerprint is None:
                    raise ValueError(
                        "production input context must be frozen before task preparation"
                    )

                if task.status == "pending":
                    raise ValueError("task dependencies are not ready")
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
                    await self._repository.freeze_prepared_task_fingerprint(
                        session,
                        task,
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


class ArtifactOverrideService:
    """Superadmin override coordinator with content-addressed storage and lease claims."""

    def __init__(
        self,
        *,
        session_factory: object,
        repository: ArtifactProductionRepository | None = None,
        catalog: ArtifactCatalog | None = None,
        store: ArtifactStore | None = None,
        validator: ArtifactValidator | None = None,
        storage_root: str | Path | None = None,
        max_override_bytes: int | None = None,
        lease_seconds: float = 60.0,
        poll_interval: float = 0.05,
        after_stage_before_store_hook: (
            Callable[[], Awaitable[None]] | None
        ) = None,
        before_cleanup_reference_check_hook: (
            Callable[[], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self._session_factory = session_factory
        if catalog is None and repository is not None:
            catalog = repository.catalog
        self._catalog = catalog or load_catalog(settings.artifact_catalog_path)
        self._repository = repository or ArtifactProductionRepository(self._catalog)
        self._store = store or ArtifactStore(
            storage_root or settings.artifact_storage_root,
            max_override_bytes=(
                settings.artifact_max_override_bytes
                if max_override_bytes is None
                else max_override_bytes
            ),
        )
        self._validator = validator or ArtifactValidator(
            max_bytes=(
                settings.artifact_max_override_bytes
                if max_override_bytes is None
                else max_override_bytes
            )
        )
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._after_stage_before_store_hook = after_stage_before_store_hook
        self._before_cleanup_reference_check_hook = (
            before_cleanup_reference_check_hook
        )

    @staticmethod
    def endpoint_for(
        event_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
    ) -> str:
        return (
            f"/api/v1/events/{event_id}/artifacts/{artifact_key}/"
            f"override?output_profile={output_profile}"
        )

    async def override(
        self,
        *,
        actor_id: str,
        event_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
        revision_id: uuid.UUID,
        reason: str,
        expected_current_artifact_id: uuid.UUID | None,
        idempotency_key: str,
        upload: BinaryIO,
    ) -> ArtifactOverrideResponse:
        if not actor_id.strip():
            raise ValueError("actor_id must not be empty")
        if not reason.strip():
            raise ValueError("reason must not be empty")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        uuid.UUID(str(idempotency_key))
        expected_current_artifact_id = _optional_uuid(
            expected_current_artifact_id,
            "expected_current_artifact_id",
        )

        context = await self._load_context(
            event_id=event_id,
            revision_id=revision_id,
            artifact_key=artifact_key,
            output_profile=output_profile,
        )
        definition = context["definition"]
        file_name = _upload_file_name(upload, definition)
        staged: Path | None = None
        stored: StoredArtifactFile | None = None
        claim: ArtifactOverrideRequest | None = None
        committed = False
        try:
            _rewind_upload(upload)
            staged = self._store.stage(upload, file_name=file_name)
            size_bytes = staged.stat().st_size
            checksum = _sha256_path(staged)
            request_fingerprint = sha256_json(
                {
                    "event_id": str(event_id),
                    "revision_id": str(revision_id),
                    "artifact_key": artifact_key,
                    "output_profile": output_profile,
                    "expected_current_artifact_id": (
                        str(expected_current_artifact_id)
                        if expected_current_artifact_id is not None
                        else None
                    ),
                    "file_name": file_name,
                    "size_bytes": size_bytes,
                    "checksum": checksum,
                    "reason": reason,
                }
            )
            claim = await self._claim_or_wait(
                actor_id=actor_id,
                event_id=event_id,
                artifact_key=artifact_key,
                output_profile=output_profile,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if claim.status == "succeeded":
                return ArtifactOverrideResponse.from_body(
                    claim.response_body or {}
                )
            validation = self._validator.validate(
                staged,
                definition,
                context["production_mode"],
            )
            if not validation.valid:
                await self._mark_failed(
                    claim.id,
                    claim.lease_generation,
                    validation,
                )
                raise ArtifactOverrideValidationError(
                    error_category=validation.error_category or "validation_failed",
                    summary=validation.summary,
                )

            object_relative_path = self._store.relative_path_for(
                checksum,
                file_name=file_name,
            )
            if self._after_stage_before_store_hook is not None:
                await self._after_stage_before_store_hook()
            async with self._object_guard(object_relative_path):
                stored = self._store.store_immutable(
                    staged,
                    file_name=file_name,
                )
                try:
                    response = await self._commit_override(
                        context=context,
                        definition=definition,
                        claim=claim,
                        stored=stored,
                        validation=validation,
                        actor_id=actor_id,
                        reason=reason,
                        expected_current_artifact_id=expected_current_artifact_id,
                    )
                    committed = True
                    return response
                except (
                    IdempotencyConflictError,
                    ArtifactOverrideLeaseError,
                ) as error:
                    await self._persist_failure(
                        claim.id,
                        claim.lease_generation,
                        error_category="conflict",
                        summary=str(error),
                        error_type=type(error).__name__,
                        response_status=409,
                    )
                    raise
                finally:
                    if stored is not None and not committed:
                        await self._delete_if_unreferenced(stored)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    async def _delete_if_unreferenced(
        self,
        stored: StoredArtifactFile,
    ) -> None:
        if self._before_cleanup_reference_check_hook is not None:
            await self._before_cleanup_reference_check_hook()
        if await self._is_storage_path_referenced(stored.relative_path):
            return
        self._store.delete_unreferenced(stored)

    async def _is_storage_path_referenced(self, relative_path: str) -> bool:
        async with self._session_factory() as session:
            artifact_count = await session.scalar(
                select(func.count())
                .select_from(GeneratedArtifact)
                .where(GeneratedArtifact.storage_path == relative_path)
            )
            publication_count = await session.scalar(
                select(func.count())
                .select_from(ArtifactPublication)
                .where(
                    ArtifactPublication.artifact_id.in_(
                        select(GeneratedArtifact.id).where(
                            GeneratedArtifact.storage_path == relative_path
                        )
                    )
                )
            )
        return bool(artifact_count) or bool(publication_count)

    @asynccontextmanager
    async def _object_guard(self, relative_path: str):
        lock_key = f"artifact-object:{relative_path}"
        async with self._session_factory() as session:
            connection = await session.connection()
            await connection.execute(
                text(
                    "SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"
                ),
                {"lock_key": lock_key},
            )
            try:
                yield
            finally:
                await connection.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": lock_key},
                )
                await session.rollback()

    async def _load_context(
        self,
        *,
        event_id: uuid.UUID,
        revision_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
    ) -> dict[str, Any]:
        definition = self._catalog.get(artifact_key, output_profile)
        async with self._session_factory() as session:
            event = await session.get(EarthquakeEvent, event_id)
            revision = await session.get(EarthquakeRevision, revision_id)
            if event is None or revision is None:
                raise LookupError("artifact override inputs were not found")
            if revision.event_id != event.id:
                raise ValueError("override revision does not belong to event")
            if (
                event.current_revision_id != revision.id
                or not revision.is_current
            ):
                raise IdempotencyConflictError(
                    "override revision is no longer current"
                )
            parent = await self._repository.get_current_full_run(
                session,
                event_id=event.id,
                revision_id=revision.id,
            )
            return {
                "event_id": event.id,
                "revision_id": revision.id,
                "revision_no": revision.revision_no,
                "production_mode": _production_mode(revision.revision_kind),
                "assessment_run_id": (
                    parent.assessment_run_id if parent is not None else None
                ),
                "definition": definition,
            }

    async def _claim_or_wait(
        self,
        *,
        actor_id: str,
        event_id: uuid.UUID,
        artifact_key: str,
        output_profile: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ArtifactOverrideRequest:
        endpoint = self.endpoint_for(event_id, artifact_key, output_profile)
        now = datetime.now(UTC)
        values = {
            "actor_id": actor_id,
            "endpoint": endpoint,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "status": "processing",
            "claimed_at": now,
            "lease_expires_at": now + timedelta(seconds=self._lease_seconds),
            "lease_generation": 1,
            "attempt_count": 1,
        }
        async with self._session_factory() as session:
            async with session.begin():
                insert_result = await session.execute(
                    pg_insert(ArtifactOverrideRequest)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            "actor_id",
                            "endpoint",
                            "idempotency_key",
                        ]
                    )
                )
                row = await session.scalar(
                    select(ArtifactOverrideRequest).where(
                        ArtifactOverrideRequest.actor_id == actor_id,
                        ArtifactOverrideRequest.endpoint == endpoint,
                        ArtifactOverrideRequest.idempotency_key
                        == idempotency_key,
                    )
                )
        if row is None:
            raise RuntimeError("override idempotency claim was not persisted")
        if insert_result.rowcount == 1:
            return row
        return await self._wait_for_existing(
            row,
            request_fingerprint=request_fingerprint,
        )

    async def _wait_for_existing(
        self,
        initial: ArtifactOverrideRequest,
        *,
        request_fingerprint: str,
    ) -> ArtifactOverrideRequest:
        deadline = datetime.now(UTC) + timedelta(
            seconds=self._lease_seconds + 5
        )
        while True:
            async with self._session_factory() as session:
                async with session.begin():
                    row = await session.get(
                        ArtifactOverrideRequest,
                        initial.id,
                        with_for_update=True,
                    )
                    if row is None:
                        raise RuntimeError("override idempotency record disappeared")
                    if row.request_fingerprint != request_fingerprint:
                        raise IdempotencyConflictError(
                            "same idempotency key was used with a different request"
                        )
                    if row.status == "succeeded":
                        return row
                    if row.status == "failed":
                        raise _override_failure(row)
                    if row.status == "processing":
                        now = datetime.now(UTC)
                        if (
                            row.lease_expires_at is None
                            or row.lease_expires_at <= now
                        ):
                            recovered = await self._recover_committed_record(
                                session,
                                row,
                            )
                            if recovered is not None:
                                return recovered
                            renewed = await self._renew_expired_lease(
                                session,
                                row,
                            )
                            if renewed is not None:
                                return renewed
                            continue
            if datetime.now(UTC) >= deadline:
                raise RuntimeError("override request is still processing")
            await asyncio.sleep(self._poll_interval)

    async def _recover_committed_record(
        self,
        session: object,
        row: ArtifactOverrideRequest,
    ) -> ArtifactOverrideRequest | None:
        if row.production_run_id is None or row.artifact_id is None:
            return None
        artifact = await session.get(GeneratedArtifact, row.artifact_id)
        publication = await session.scalar(
            select(ArtifactPublication)
            .where(
                ArtifactPublication.artifact_id == row.artifact_id,
                ArtifactPublication.production_run_id == row.production_run_id,
            )
            .order_by(ArtifactPublication.published_at.desc())
            .limit(1)
        )
        run = await session.get(ProductionRun, row.production_run_id)
        if artifact is None or publication is None or run is None:
            return None
        response = ArtifactOverrideResponse(
            artifact_id=artifact.id,
            production_run_id=run.id,
            production_task_id=artifact.production_task_id,
            artifact_publication_id=publication.id,
            status=run.status,
            generation_seq=run.generation_seq,
            file_name=artifact.file_name,
            checksum=artifact.checksum,
            size_bytes=artifact.size_bytes,
            generated_at=artifact.generated_at,
        )
        row.status = "succeeded"
        row.response_status = 200
        row.response_body = response.to_body()
        row.production_run_id = run.id
        row.artifact_id = artifact.id
        row.completed_at = datetime.now(UTC)
        await session.flush()
        return row

    async def _renew_expired_lease(
        self,
        session: object,
        row: ArtifactOverrideRequest,
    ) -> ArtifactOverrideRequest | None:
        now = datetime.now(UTC)
        result = await session.execute(
            text(
                """
                UPDATE artifact_override_requests
                SET lease_generation = lease_generation + 1,
                    attempt_count = attempt_count + 1,
                    claimed_at = :claimed_at,
                    lease_expires_at = :lease_expires_at
                WHERE id = :id
                  AND status = 'processing'
                  AND lease_expires_at < :now
                RETURNING id
                """
            ),
            {
                "id": row.id,
                "claimed_at": now,
                "lease_expires_at": now
                + timedelta(seconds=self._lease_seconds),
                "now": now,
            },
        )
        if result.rowcount != 1:
            return None
        refreshed = await session.get(ArtifactOverrideRequest, row.id)
        if refreshed is not None:
            await session.refresh(refreshed)
        return refreshed

    async def _mark_failed(
        self,
        request_id: uuid.UUID,
        lease_generation: int,
        validation: ValidationResult,
    ) -> None:
        await self._persist_failure(
            request_id,
            lease_generation,
            error_category=validation.error_category or "validation_failed",
            summary=validation.summary,
            error_type="ArtifactOverrideValidationError",
            response_status=422,
        )

    async def _persist_failure(
        self,
        request_id: uuid.UUID,
        lease_generation: int,
        *,
        error_category: str,
        summary: str,
        error_type: str,
        response_status: int,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    ArtifactOverrideRequest,
                    request_id,
                    with_for_update=True,
                )
                if row is None or row.status == "succeeded":
                    return
                if row.status == "failed":
                    return
                if row.lease_generation != lease_generation:
                    return
                row.status = "failed"
                row.response_status = response_status
                row.response_body = {
                    "error_category": error_category,
                    "summary": summary,
                    "error_type": error_type,
                }
                row.completed_at = datetime.now(UTC)

    async def _commit_override(
        self,
        *,
        context: Mapping[str, Any],
        definition: ArtifactDefinition,
        claim: ArtifactOverrideRequest,
        stored: StoredArtifactFile,
        validation: ValidationResult,
        actor_id: str,
        reason: str,
        expected_current_artifact_id: uuid.UUID | None,
    ) -> ArtifactOverrideResponse:
        now = datetime.now(UTC)
        event_id = uuid.UUID(str(context["event_id"]))
        revision_id = uuid.UUID(str(context["revision_id"]))
        production_mode = str(context["production_mode"])
        assessment_run_id = context.get("assessment_run_id")
        artifact_key = definition.artifact_key
        output_profile = definition.output_profile

        async with self._session_factory() as session:
            async with session.begin():
                event = await session.get(
                    EarthquakeEvent,
                    event_id,
                    with_for_update=True,
                )
                if event is None or event.current_revision_id != revision_id:
                    raise IdempotencyConflictError(
                        "override revision is no longer current"
                    )
                if assessment_run_id is not None:
                    assessment = await session.get(
                        AssessmentRun,
                        assessment_run_id,
                        with_for_update=True,
                    )
                    if assessment is None:
                        raise LookupError("override assessment run not found")

                artifact_result = ArtifactGenerationResult(
                    file_name=stored.file_name,
                    format=definition.format,
                    storage_path=stored.relative_path,
                    checksum=stored.checksum,
                    size_bytes=stored.size_bytes,
                    quality=ArtifactQuality(
                        grade=definition.quality_policy,
                        needs_review=False,
                    ),
                    width=(
                        validation.dimensions[0]
                        if validation.dimensions is not None
                        else None
                    ),
                    height=(
                        validation.dimensions[1]
                        if validation.dimensions is not None
                        else None
                    ),
                    page_count=validation.page_count,
                    generated_at=now,
                    publication_mode="superadmin_override",
                    render_manifest={
                        "override": {
                            "actor_id": actor_id,
                            "reason": reason,
                            "request_fingerprint": claim.request_fingerprint,
                        }
                    },
                )
                command = CreateProductionRunCommand(
                    assessment_run_id=assessment_run_id,
                    event_id=event.id,
                    revision_id=revision_id,
                    revision_no=int(context["revision_no"]),
                    production_mode=production_mode,
                    launch_mode="standalone",
                    deadline_basis_at=now,
                    deadline_at=now + timedelta(seconds=300),
                    deadline_kind="rebuild_deadline",
                    catalog_version=self._catalog.catalog_version,
                    generation_scope=f"artifact:{artifact_key}:{output_profile}",
                    required_outputs=((artifact_key, output_profile),),
                    snapshot={
                        "launch": "standalone",
                        "source": "superadmin_override",
                        "requested_by": actor_id,
                        "reason": reason,
                        "artifact_key": artifact_key,
                        "output_profile": output_profile,
                        "request_fingerprint": claim.request_fingerprint,
                    },
                    artifact_result=artifact_result,
                    published_by=actor_id,
                    forced=True,
                )
                publication_check_started_at = datetime.now(UTC)
                run, task, artifact, publication = (
                    await self._repository.create_override_run(session, command)
                )
                if expected_current_artifact_id is not None:
                    superseded = await session.scalar(
                        select(ArtifactPublication)
                        .where(
                            ArtifactPublication.event_id == event.id,
                            ArtifactPublication.artifact_key == artifact_key,
                            ArtifactPublication.output_profile == output_profile,
                            ArtifactPublication.production_mode == production_mode,
                            ArtifactPublication.superseded_at
                            >= publication_check_started_at,
                        )
                        .order_by(
                            ArtifactPublication.superseded_at.desc(),
                            ArtifactPublication.id.desc(),
                        )
                        .limit(1)
                    )
                    if (
                        superseded is None
                        or superseded.artifact_id != expected_current_artifact_id
                    ):
                        raise IdempotencyConflictError(
                            "expected current artifact id does not match publication"
                        )

                response = ArtifactOverrideResponse(
                    artifact_id=artifact.id,
                    production_run_id=run.id,
                    production_task_id=task.id,
                    artifact_publication_id=publication.id,
                    status=run.status,
                    generation_seq=run.generation_seq,
                    file_name=artifact.file_name,
                    checksum=artifact.checksum,
                    size_bytes=artifact.size_bytes,
                    generated_at=artifact.generated_at,
                )
                request_row = await session.scalar(
                    select(ArtifactOverrideRequest)
                    .where(ArtifactOverrideRequest.id == claim.id)
                    .with_for_update()
                )
                if (
                    request_row is None
                    or request_row.status != "processing"
                    or request_row.lease_generation != claim.lease_generation
                ):
                    raise ArtifactOverrideLeaseError(
                        "override lease generation changed before commit"
                    )
                request_row.status = "succeeded"
                request_row.response_status = 200
                request_row.response_body = response.to_body()
                request_row.production_run_id = run.id
                request_row.artifact_id = artifact.id
                request_row.completed_at = datetime.now(UTC)
                return response


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


def _optional_uuid(
    value: uuid.UUID | str | None,
    field_name: str,
) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _upload_file_name(
    upload: BinaryIO,
    definition: ArtifactDefinition,
) -> str:
    raw_name = getattr(upload, "name", None)
    if raw_name:
        candidate = Path(str(raw_name)).name
        if candidate not in {"", ".", ".."}:
            return candidate
    return f"{definition.artifact_key}.{definition.format}"


def _rewind_upload(upload: BinaryIO) -> None:
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        return


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _override_failure(row: ArtifactOverrideRequest) -> ArtifactOverrideValidationError:
    body = dict(row.response_body or {})
    error_type = str(body.get("error_type") or "")
    summary = str(body.get("summary") or "artifact override request failed")
    if error_type in {
        "IdempotencyConflictError",
        "ArtifactOverrideLeaseError",
        "conflict",
    }:
        raise IdempotencyConflictError(summary)
    return ArtifactOverrideValidationError(
        error_category=str(body.get("error_category") or "validation_failed"),
        summary=summary,
    )


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
