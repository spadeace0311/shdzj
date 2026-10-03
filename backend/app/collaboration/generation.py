"""Idempotent generation of workgroup tasks from event lifecycle outboxes."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collaboration.domain import WorkgroupCode
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    CollaborationTaskTemplate,
    CollaborationTaskTemplateVersion,
    WorkgroupTask,
)
from app.collaboration.templates import (
    ArtifactBinding,
    TaskTemplateCatalog,
    TaskTemplateDefinition,
    load_task_template_catalog,
)
from app.config import settings
from app.events.domain import EventKind
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
)

_COLLABORATION_TRIGGER_TYPE = "collaboration.requested"
_COLLABORATION_UPGRADE_TRIGGER_TYPE = "collaboration.upgrade_requested"
_COLLABORATION_TRIGGER_TYPES = {
    _COLLABORATION_TRIGGER_TYPE,
    _COLLABORATION_UPGRADE_TRIGGER_TYPE,
}
_SYSTEM_ACTOR = "system"


@dataclass(frozen=True, slots=True)
class GenerationResult:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    created: int = 0
    existing: int = 0
    not_required: int = 0
    reactivated: int = 0
    task_events: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.not_required or self.reactivated)


@dataclass(frozen=True, slots=True)
class _ClaimedOutbox:
    id: uuid.UUID
    event_id: uuid.UUID
    revision_id: uuid.UUID
    attempt_count: int


class CollaborationTaskGenerator:
    def __init__(
        self,
        *,
        catalog: TaskTemplateCatalog | None = None,
        catalog_path: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if catalog is not None and catalog_path is not None:
            raise ValueError("provide catalog or catalog_path, not both")
        self._catalog = catalog
        self._catalog_path = catalog_path
        self._now = now

    @property
    def catalog(self) -> TaskTemplateCatalog:
        if self._catalog is None:
            self._catalog = load_task_template_catalog(
                self._catalog_path or settings.collaboration_task_template_path
            )
        return self._catalog

    async def generate_for_revision(
        self,
        session: AsyncSession,
        event_id: object,
        revision_id: object,
        outbox_id: object,
    ) -> GenerationResult:
        event_uuid = _coerce_uuid(event_id, "event_id")
        revision_uuid = _coerce_uuid(revision_id, "revision_id")
        outbox_uuid = _coerce_uuid(outbox_id, "outbox_id")

        event = await session.get(
            EarthquakeEvent,
            event_uuid,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("collaboration event not found")
        outbox = await session.get(
            EventLifecycleOutbox,
            outbox_uuid,
            with_for_update=True,
        )
        if outbox is None:
            raise LookupError("collaboration outbox not found")
        if (
            outbox.trigger_type not in _COLLABORATION_TRIGGER_TYPES
            or outbox.event_id != event_uuid
            or outbox.revision_id != revision_uuid
        ):
            raise ValueError("collaboration outbox identity mismatch")
        revision = await session.get(
            EarthquakeRevision,
            revision_uuid,
            with_for_update=True,
        )
        if revision is None or revision.event_id != event.id:
            raise ValueError("collaboration revision does not belong to event")

        if EventKind(revision.revision_kind) is EventKind.AUTO:
            return GenerationResult(event_id=event.id, revision_id=revision.id)
        if not revision.is_current:
            return GenerationResult(event_id=event.id, revision_id=revision.id)

        observed_at = _normalize_utc(outbox.created_at, "outbox.created_at")
        threshold = _payload_threshold(outbox.payload)
        explicit_version = _explicit_template_version(outbox)
        if (
            outbox.trigger_type == _COLLABORATION_UPGRADE_TRIGGER_TYPE
            and explicit_version is None
        ):
            raise ValueError(
                "explicit template upgrade requires payload.template_version"
            )
        if (
            explicit_version is not None
            and explicit_version != self.catalog.version
        ):
            raise ValueError(
                "explicit template upgrade must match the active catalog version"
            )

        frozen_version = explicit_version or await self._frozen_template_version(
            session,
            event.id,
        )
        catalog = (
            self.catalog
            if frozen_version == self.catalog.version
            else await self._persisted_catalog(session, frozen_version)
        )
        definitions = catalog.get_applicable(
            event,
            revision,
            intensity_threshold=threshold,
        )

        catalog_versions = await self._ensure_catalog_versions(
            session,
            observed_at=observed_at,
            catalog=catalog,
        )
        existing_tasks = (
            await session.scalars(
                select(WorkgroupTask)
                .where(
                    WorkgroupTask.event_id == event.id,
                    WorkgroupTask.template_version_id.is_not(None),
                )
                .with_for_update()
            )
        ).all()
        frozen_version_ids = set(catalog_versions.values())
        existing_tasks = [
            task
            for task in existing_tasks
            if task.template_version_id in frozen_version_ids
        ]
        tasks_by_key = {
            (task.template_version_id, task.task_code): task
            for task in existing_tasks
        }
        applicable_keys = {
            (catalog_versions[definition.template_code], definition.template_code)
            for definition in definitions
        }
        timing_basis = _task_timing_basis(event, revision, observed_at)
        source_type = (
            "correction"
            if EventKind(revision.revision_kind) is EventKind.CORRECTION
            else "preplan"
        )

        created = 0
        existing = 0
        not_required = 0
        reactivated = 0
        task_events = 0

        for definition in definitions:
            template_version_id = catalog_versions[definition.template_code]
            task = tasks_by_key.get(
                (template_version_id, definition.template_code)
            )
            if task is None:
                task = self._new_task(
                    event=event,
                    revision=revision,
                    template_version_id=template_version_id,
                    definition=definition,
                    source_type=source_type,
                    timing_basis=timing_basis,
                    observed_at=observed_at,
                )
                session.add(task)
                await session.flush()
                task_events += await _append_task_event(
                    session,
                    task,
                    event_type="task_generated",
                    from_status=None,
                    to_status=task.status,
                    outbox_id=outbox.id,
                    occurred_at=observed_at,
                )
                created += 1
                continue

            if task.status == "not_required":
                previous_status = task.status
                task.status = "pending"
                task.closed_at = None
                task.trigger_revision_id = revision.id
                task.row_version += 1
                task.updated_at = observed_at
                task_events += await _append_task_event(
                    session,
                    task,
                    event_type="task_reactivated",
                    from_status=previous_status,
                    to_status=task.status,
                    outbox_id=outbox.id,
                    occurred_at=observed_at,
                )
                reactivated += 1
            else:
                existing += 1

        for task in existing_tasks:
            task_key = (task.template_version_id, task.task_code)
            if task.status != "pending" or task_key in applicable_keys:
                continue
            previous_status = task.status
            task.status = "not_required"
            task.closed_at = observed_at
            task.row_version += 1
            task.updated_at = observed_at
            task_events += await _append_task_event(
                session,
                task,
                event_type="task_not_required",
                from_status=previous_status,
                to_status=task.status,
                outbox_id=outbox.id,
                occurred_at=observed_at,
            )
            not_required += 1

        await _enqueue_projection(
            session,
            event_id=event.id,
            revision_id=revision.id,
            observed_at=observed_at,
        )

        return GenerationResult(
            event_id=event.id,
            revision_id=revision.id,
            created=created,
            existing=existing,
            not_required=not_required,
            reactivated=reactivated,
            task_events=task_events,
        )

    async def _ensure_catalog_versions(
        self,
        session: AsyncSession,
        *,
        observed_at: datetime,
        catalog: TaskTemplateCatalog | None = None,
    ) -> dict[str, uuid.UUID]:
        catalog = catalog or self.catalog
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_key, 0))"
            ),
            {"lock_key": f"collaboration-template:{catalog.version}"},
        )
        versions: dict[str, uuid.UUID] = {}
        for definition in catalog.definitions:
            template = await session.scalar(
                select(CollaborationTaskTemplate)
                .where(
                    CollaborationTaskTemplate.code
                    == definition.template_code
                )
                .with_for_update()
            )
            if template is None:
                template = CollaborationTaskTemplate(
                    code=definition.template_code,
                    title=definition.title,
                    category=definition.source,
                    workgroup_code=definition.workgroup_code.value,
                    is_active=True,
                )
                session.add(template)
                await session.flush()

            version = await session.scalar(
                select(CollaborationTaskTemplateVersion)
                .where(
                    CollaborationTaskTemplateVersion.template_code
                    == definition.template_code,
                    CollaborationTaskTemplateVersion.version
                    == catalog.version,
                )
                .with_for_update()
            )
            if version is None:
                version = CollaborationTaskTemplateVersion(
                    template_id=template.id,
                    version=catalog.version,
                    template_code=definition.template_code,
                    phase_code=definition.phase_code,
                    start_offset_seconds=definition.start_offset_seconds,
                    due_offset_seconds=definition.due_offset_seconds,
                    continues_until_response_end=(
                        definition.continues_until_response_end
                    ),
                    priority=definition.priority,
                    required_deliverables=list(
                        definition.required_deliverables
                    ),
                    optional_deliverables=[],
                    artifact_bindings=[
                        {
                            "artifact_key": binding.artifact_key,
                            "output_profile": binding.output_profile,
                        }
                        for binding in definition.artifact_bindings
                    ],
                    applicability=_json_compatible(
                        dict(definition.applicability)
                    ),
                    instruction=definition.instruction,
                    response_basis=definition.response_basis,
                    published_at=observed_at,
                    created_by=_SYSTEM_ACTOR,
                )
                session.add(version)
                await session.flush()
            versions[definition.template_code] = version.id
        return versions

    async def _frozen_template_version(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> str:
        row = (
            await session.execute(
                select(
                    CollaborationTaskTemplateVersion.version,
                    func.max(WorkgroupTask.created_at),
                )
                .join(
                    WorkgroupTask,
                    WorkgroupTask.template_version_id
                    == CollaborationTaskTemplateVersion.id,
                )
                .where(
                    WorkgroupTask.event_id == event_id,
                    WorkgroupTask.template_version_id.is_not(None),
                )
                .group_by(CollaborationTaskTemplateVersion.version)
                .order_by(
                    func.max(WorkgroupTask.created_at).desc(),
                    CollaborationTaskTemplateVersion.version.desc(),
                )
                .limit(1)
            )
        ).first()
        return str(row[0]) if row is not None else self.catalog.version

    async def _persisted_catalog(
        self,
        session: AsyncSession,
        version_name: str,
    ) -> TaskTemplateCatalog:
        rows = (
            await session.execute(
                select(
                    CollaborationTaskTemplateVersion,
                    CollaborationTaskTemplate,
                )
                .join(
                    CollaborationTaskTemplate,
                    CollaborationTaskTemplate.id
                    == CollaborationTaskTemplateVersion.template_id,
                )
                .where(
                    CollaborationTaskTemplateVersion.version == version_name,
                )
                .order_by(CollaborationTaskTemplateVersion.template_code)
            )
        ).all()
        if not rows:
            raise ValueError(
                f"frozen task template version is unavailable: {version_name}"
            )
        return TaskTemplateCatalog(
            version=version_name,
            definitions=tuple(
                _definition_from_version(version, template)
                for version, template in rows
            ),
        )

    @staticmethod
    def _new_task(
        *,
        event: EarthquakeEvent,
        revision: EarthquakeRevision,
        template_version_id: uuid.UUID,
        definition: TaskTemplateDefinition,
        source_type: str,
        timing_basis: datetime,
        observed_at: datetime,
    ) -> WorkgroupTask:
        return WorkgroupTask(
            event_id=event.id,
            trigger_revision_id=revision.id,
            template_version_id=template_version_id,
            task_code=definition.template_code,
            source_type=source_type,
            source_ref=(
                definition.response_basis[:256]
                if definition.response_basis is not None
                else None
            ),
            workgroup_code=definition.workgroup_code.value,
            title=definition.title,
            instruction=definition.instruction,
            priority=definition.priority,
            status="pending",
            timeliness_state="on_time",
            phase_code=definition.phase_code,
            activated_at=timing_basis
            + timedelta(seconds=definition.start_offset_seconds),
            due_at=(
                timing_basis
                + timedelta(seconds=definition.due_offset_seconds)
                if definition.due_offset_seconds is not None
                else None
            ),
            row_version=1,
            created_by=_SYSTEM_ACTOR,
            created_at=observed_at,
            updated_at=observed_at,
        )


class CollaborationOutboxDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        catalog_path: str | None = None,
        generator: CollaborationTaskGenerator | None = None,
        batch_size: int = 20,
        max_attempts: int = 10,
        lease_seconds: int = 60,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._session_factory = session_factory
        self._generator = generator or CollaborationTaskGenerator(
            catalog_path=catalog_path,
            now=now,
        )
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._now = now

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    async def dispatch_once(self) -> int:
        claimed = await self._claim_pending()
        published = 0
        for item in claimed:
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        await self._generator.generate_for_revision(
                            session,
                            item.event_id,
                            item.revision_id,
                            item.id,
                        )
            except Exception as exc:
                await self._record_failure(
                    item.id,
                    item.attempt_count,
                    exc,
                )
            else:
                await self._record_success(item.id, item.attempt_count)
                published += 1
        return published

    async def _claim_pending(self) -> list[_ClaimedOutbox]:
        now = _normalize_utc(self._now(), "now")
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(EventLifecycleOutbox)
                        .where(
                            EventLifecycleOutbox.trigger_type
                            .in_(tuple(_COLLABORATION_TRIGGER_TYPES)),
                            EventLifecycleOutbox.status.in_(
                                ("pending", "processing")
                            ),
                            EventLifecycleOutbox.available_at <= now,
                        )
                        .order_by(
                            EventLifecycleOutbox.available_at,
                            EventLifecycleOutbox.created_at,
                            EventLifecycleOutbox.id,
                        )
                        .limit(self._batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                claimed: list[_ClaimedOutbox] = []
                for row in rows:
                    row.status = "processing"
                    row.attempt_count += 1
                    row.available_at = lease_until
                    claimed.append(
                        _ClaimedOutbox(
                            id=row.id,
                            event_id=row.event_id,
                            revision_id=row.revision_id,
                            attempt_count=row.attempt_count,
                        )
                    )
                return claimed

    async def _record_success(
        self,
        outbox_id: uuid.UUID,
        expected_attempt_count: int,
    ) -> None:
        now = _normalize_utc(self._now(), "now")
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(
                    EventLifecycleOutbox,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    outbox,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                outbox.status = "published"
                outbox.published_at = now
                outbox.last_error = None

    async def _record_failure(
        self,
        outbox_id: uuid.UUID,
        expected_attempt_count: int,
        exc: Exception,
    ) -> None:
        now = _normalize_utc(self._now(), "now")
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(
                    EventLifecycleOutbox,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    outbox,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                outbox.last_error = _safe_error_text(exc)
                if outbox.attempt_count >= self._max_attempts:
                    outbox.status = "dead_letter"
                    outbox.available_at = now
                    return
                outbox.status = "pending"
                outbox.available_at = now + timedelta(
                    seconds=min(2 ** max(outbox.attempt_count - 1, 0), 300)
                )


async def _append_task_event(
    session: AsyncSession,
    task: WorkgroupTask,
    *,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    outbox_id: uuid.UUID,
    occurred_at: datetime,
) -> int:
    next_sequence = int(
        await session.scalar(
            select(func.max(CollaborationTaskEvent.event_seq)).where(
                CollaborationTaskEvent.task_id == task.id
            )
        )
        or 0
    ) + 1
    session.add(
        CollaborationTaskEvent(
            task_id=task.id,
            event_seq=next_sequence,
            event_type=event_type,
            actor=_SYSTEM_ACTOR,
            from_status=from_status,
            to_status=to_status,
            business_version=task.row_version,
            idempotency_key=_task_event_key(
                outbox_id,
                task.id,
                event_type,
            ),
            payload={
                "outbox_id": str(outbox_id),
                "task_code": task.task_code,
            },
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return 1


def _payload_threshold(payload: Mapping[str, Any]) -> Decimal | None:
    value = payload.get("intensity_threshold")
    if value is None:
        return None
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("intensity_threshold payload is not numeric") from exc
    if not threshold.is_finite() or threshold < 0:
        raise ValueError("intensity_threshold payload must be non-negative")
    return threshold


def _explicit_template_version(
    outbox: EventLifecycleOutbox,
) -> str | None:
    value = outbox.payload.get("template_version")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("template_version payload must be a non-empty string")
    return value.strip()


def _definition_from_version(
    version: CollaborationTaskTemplateVersion,
    template: CollaborationTaskTemplate,
) -> TaskTemplateDefinition:
    return TaskTemplateDefinition(
        template_code=version.template_code,
        workgroup_code=WorkgroupCode(template.workgroup_code),
        phase_code=version.phase_code,
        title=template.title or version.template_code,
        source=(version.response_basis or "frozen template"),
        start_offset_seconds=version.start_offset_seconds,
        due_offset_seconds=version.due_offset_seconds,
        continues_until_response_end=version.continues_until_response_end,
        required_deliverables=tuple(
            str(item) for item in (version.required_deliverables or ())
        ),
        artifact_bindings=tuple(
            ArtifactBinding(
                artifact_key=str(item["artifact_key"]),
                output_profile=str(item["output_profile"]),
            )
            for item in (version.artifact_bindings or ())
            if isinstance(item, Mapping)
            and item.get("artifact_key")
            and item.get("output_profile")
        ),
        applicability=MappingProxyType(dict(version.applicability or {})),
        priority=version.priority,
        instruction=version.instruction,
        response_basis=version.response_basis,
    )


async def _enqueue_projection(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
    observed_at: datetime,
) -> None:
    key = f"collaboration-generated:{event_id}:{revision_id}"
    existing = await session.scalar(
        select(CollaborationOutbox.id).where(
            CollaborationOutbox.idempotency_key == key
        )
    )
    if existing is not None:
        return
    session.add(
        CollaborationOutbox(
            event_id=event_id,
            task_id=None,
            event_type="task.generated",
            idempotency_key=key,
            payload={
                "event_id": str(event_id),
                "revision_id": str(revision_id),
            },
            status="pending",
            attempt_count=0,
            available_at=observed_at,
            created_at=observed_at,
            updated_at=observed_at,
        )
    )
    await session.flush()


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_compatible(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _task_timing_basis(
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
    fallback: datetime,
) -> datetime:
    basis = event.origin_time or revision.ingested_at or fallback
    return _normalize_utc(basis, "task timing basis")


def _task_event_key(
    outbox_id: uuid.UUID,
    task_id: uuid.UUID,
    event_type: str,
) -> str:
    material = f"{outbox_id}:{task_id}:{event_type}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _normalize_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


def _is_current_attempt(
    outbox: EventLifecycleOutbox | None,
    *,
    expected_attempt_count: int,
) -> bool:
    return (
        outbox is not None
        and outbox.status == "processing"
        and outbox.attempt_count == expected_attempt_count
    )


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]
