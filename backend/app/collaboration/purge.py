from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Index,
    String,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.storage import ArtifactStore
from app.assessment.models import AssessmentRun, AssessmentTask
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    CommandHallAlertProjection,
    CommandHallEventProjection,
    CommandHallGroupProjection,
    NotificationDelivery,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupAttendance,
    WorkgroupRosterSnapshot,
    WorkgroupTask,
    WorkgroupTaskContributor,
)
from app.db import Base
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.event_object_locks import (
    lock_artifact_object,
    lock_event_write,
)
from app.event_object_cleanup import (
    LEGACY_STORAGE_NAMESPACE,
    cleanup_event_objects,
)


class EventNotFoundError(LookupError):
    """Raised when an event has never existed or lacks the matching receipt."""


class PurgeNamespaceUnavailableError(RuntimeError):
    """Raised when a purge replay lacks a safe frozen storage namespace."""


class EventPurgeReceipt(Base):
    __tablename__ = "event_purge_receipts"
    __table_args__ = (
        Index(
            "uq_event_purge_receipt_event_key",
            "event_id",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_event_purge_receipts_purged_at", "purged_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    actor: Mapped[str] = mapped_column(String(64))
    deletion_counts: Mapped[dict] = mapped_column(JSONB)
    storage_paths: Mapped[list] = mapped_column(JSONB)
    storage_namespace: Mapped[str | None] = mapped_column(String(128))
    purged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


@dataclass(frozen=True, slots=True)
class PurgeResult:
    deleted_event_id: uuid.UUID
    deleted_revision_count: int
    deleted_raw_message_count: int
    deleted_assessment_run_count: int
    deleted_assessment_task_count: int
    deleted_collaboration_task_count: int
    deleted_artifact_count: int
    deleted_publication_count: int
    deleted_override_count: int
    deleted_task_event_count: int
    storage_namespace: str
    storage_paths: tuple[str, ...] = ()
    already_purged: bool = False


class SuperadminPurgeService:
    async def purge_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        actor: object,
        *,
        storage_namespace: str,
        idempotency_key: str | None = None,
    ) -> PurgeResult:
        if getattr(actor, "role", None) != "superadmin":
            raise PermissionError("Insufficient permissions")

        namespace = _normalize_storage_namespace(storage_namespace)
        key = _idempotency_key(event_id, idempotency_key)
        await lock_event_write(session, event_id, exclusive=True)
        event = await session.scalar(
            select(EarthquakeEvent).where(EarthquakeEvent.id == event_id).with_for_update()
        )
        existing_receipt = await session.scalar(
            select(EventPurgeReceipt)
            .where(
                EventPurgeReceipt.event_id == event_id,
                EventPurgeReceipt.idempotency_key == key,
            )
            .with_for_update()
        )
        if existing_receipt is not None:
            frozen_namespace = _require_receipt_namespace(existing_receipt)
            if frozen_namespace != namespace:
                raise PurgeNamespaceUnavailableError(
                    "event purge receipt storage namespace does not match current root"
                )
            return _result_from_receipt(existing_receipt)
        if event is None:
            raise EventNotFoundError("event_not_found")

        revision_rows = (
            await session.execute(
                select(
                    EarthquakeRevision.id,
                    EarthquakeRevision.raw_message_id,
                )
                .where(EarthquakeRevision.event_id == event_id)
                .with_for_update()
            )
        ).all()
        revision_ids = [row.id for row in revision_rows]
        raw_message_ids = list({row.raw_message_id for row in revision_rows})

        assessment_run_ids = list(
            (
                await session.scalars(
                    select(AssessmentRun.id)
                    .where(AssessmentRun.event_id == event_id)
                    .with_for_update()
                )
            ).all()
        )
        assessment_task_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AssessmentTask)
                .where(AssessmentTask.run_id.in_(assessment_run_ids))
            )
            or 0
        )
        workgroup_task_ids = list(
            (
                await session.scalars(
                    select(WorkgroupTask.id)
                    .where(WorkgroupTask.event_id == event_id)
                    .with_for_update()
                )
            ).all()
        )
        deliverable_ids = list(
            (
                await session.scalars(
                    select(TaskDeliverable.id)
                    .where(TaskDeliverable.task_id.in_(workgroup_task_ids))
                    .with_for_update()
                )
            ).all()
        )
        await session.execute(
            select(ProductionRun.id).where(ProductionRun.event_id == event_id).with_for_update()
        )
        artifact_ids = list(
            (
                await session.scalars(
                    select(GeneratedArtifact.id)
                    .where(GeneratedArtifact.event_id == event_id)
                    .with_for_update()
                )
            ).all()
        )
        publication_ids = list(
            (
                await session.scalars(
                    select(ArtifactPublication.id)
                    .where(ArtifactPublication.event_id == event_id)
                    .with_for_update()
                )
            ).all()
        )
        task_event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(CollaborationTaskEvent)
                .where(CollaborationTaskEvent.task_id.in_(workgroup_task_ids))
            )
            or 0
        )
        override_ids = list(
            (
                await session.scalars(
                    select(ArtifactOverrideRequest.id)
                    .where(ArtifactOverrideRequest.event_id == event_id)
                    .with_for_update()
                )
            ).all()
        )

        artifact_paths = set(
            (
                await session.scalars(
                    select(GeneratedArtifact.storage_path).where(
                        GeneratedArtifact.event_id == event_id
                    )
                )
            ).all()
        )
        manual_paths = set(
            (
                await session.scalars(
                    select(TaskDeliverableVersion.storage_key).where(
                        TaskDeliverableVersion.deliverable_id.in_(deliverable_ids),
                        TaskDeliverableVersion.storage_key.is_not(None),
                    )
                )
            ).all()
        )
        candidate_paths = {path for path in artifact_paths | manual_paths if path}
        for storage_path in sorted(candidate_paths):
            await lock_artifact_object(session, storage_path)

        if override_ids:
            await session.execute(
                delete(ArtifactOverrideRequest).where(ArtifactOverrideRequest.id.in_(override_ids))
            )
        if task_event_count:
            await session.execute(
                delete(CollaborationTaskEvent).where(
                    CollaborationTaskEvent.task_id.in_(workgroup_task_ids)
                )
            )
        await session.execute(
            delete(NotificationDelivery).where(
                (NotificationDelivery.event_id == event_id)
                | (NotificationDelivery.task_id.in_(workgroup_task_ids))
            )
        )
        await session.execute(
            delete(CollaborationOutbox).where(
                (CollaborationOutbox.event_id == event_id)
                | (CollaborationOutbox.task_id.in_(workgroup_task_ids))
            )
        )
        await session.execute(
            delete(CommandHallAlertProjection).where(
                CommandHallAlertProjection.event_id == event_id
            )
        )
        await session.execute(
            delete(CommandHallGroupProjection).where(
                CommandHallGroupProjection.event_id == event_id
            )
        )
        await session.execute(
            delete(CommandHallEventProjection).where(
                CommandHallEventProjection.event_id == event_id
            )
        )
        if deliverable_ids:
            await session.execute(
                delete(TaskDeliverablePublication).where(
                    TaskDeliverablePublication.deliverable_id.in_(deliverable_ids)
                )
            )
            await session.execute(
                delete(TaskDeliverableVersion).where(
                    TaskDeliverableVersion.deliverable_id.in_(deliverable_ids)
                )
            )
            await session.execute(
                delete(TaskDeliverable).where(TaskDeliverable.id.in_(deliverable_ids))
            )
        if workgroup_task_ids:
            await session.execute(
                delete(WorkgroupTaskContributor).where(
                    WorkgroupTaskContributor.task_id.in_(workgroup_task_ids)
                )
            )
        await session.execute(
            delete(WorkgroupAttendance).where(WorkgroupAttendance.event_id == event_id)
        )
        await session.execute(
            delete(WorkgroupRosterSnapshot).where(WorkgroupRosterSnapshot.event_id == event_id)
        )
        await session.execute(delete(WorkgroupTask).where(WorkgroupTask.event_id == event_id))
        await session.execute(
            delete(ArtifactPublication).where(ArtifactPublication.event_id == event_id)
        )
        if artifact_ids:
            await session.execute(
                update(ProductionTask)
                .where(ProductionTask.final_artifact_id.in_(artifact_ids))
                .values(final_artifact_id=None)
            )
            await session.execute(
                delete(GeneratedArtifact).where(GeneratedArtifact.event_id == event_id)
            )
        await session.execute(delete(ProductionRun).where(ProductionRun.event_id == event_id))
        await session.execute(delete(AssessmentRun).where(AssessmentRun.event_id == event_id))
        await session.execute(
            delete(EventLifecycleOutbox).where(EventLifecycleOutbox.event_id == event_id)
        )
        await session.execute(
            delete(EarthquakeRevision).where(EarthquakeRevision.event_id == event_id)
        )
        if raw_message_ids:
            await session.execute(delete(RawMessage).where(RawMessage.id.in_(raw_message_ids)))
        await session.delete(event)
        await session.flush()

        storage_paths = tuple(sorted(candidate_paths))
        result = PurgeResult(
            deleted_event_id=event_id,
            deleted_revision_count=len(revision_ids),
            deleted_raw_message_count=len(raw_message_ids),
            deleted_assessment_run_count=len(assessment_run_ids),
            deleted_assessment_task_count=assessment_task_count,
            deleted_collaboration_task_count=len(workgroup_task_ids),
            deleted_artifact_count=len(artifact_ids),
            deleted_publication_count=len(publication_ids),
            deleted_override_count=len(override_ids),
            deleted_task_event_count=task_event_count,
            storage_paths=storage_paths,
            storage_namespace=namespace,
        )
        session.add(
            EventPurgeReceipt(
                event_id=event_id,
                idempotency_key=key,
                actor=str(getattr(actor, "username", "unknown")),
                deletion_counts=_result_counts(result),
                storage_paths=list(storage_paths),
                storage_namespace=namespace,
            )
        )
        await session.flush()
        return result


async def cleanup_purged_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    storage_paths: tuple[str, ...],
    artifact_store: ArtifactStore,
    *,
    expected_storage_namespace: str,
) -> bool:
    try:
        expected_namespace = _normalize_storage_namespace(
            expected_storage_namespace,
        )
        current_namespace = _normalize_storage_namespace(
            getattr(artifact_store, "storage_namespace", None),
        )
    except ValueError:
        return False
    if (
        expected_namespace == LEGACY_STORAGE_NAMESPACE
        or current_namespace == LEGACY_STORAGE_NAMESPACE
        or expected_namespace != current_namespace
    ):
        return False
    result = await cleanup_event_objects(
        session,
        event_id,
        storage_paths,
        artifact_store,
        exclusive_event_lock=True,
    )
    return result.succeeded


def _idempotency_key(
    event_id: uuid.UUID,
    idempotency_key: str | None,
) -> str:
    if idempotency_key is None:
        return f"event:{event_id}"
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must not be empty")
    key = idempotency_key.strip()
    if len(key) > 160:
        raise ValueError("idempotency_key is too long")
    return key


def _result_counts(result: PurgeResult) -> dict[str, int]:
    return {
        key: value
        for key, value in asdict(result).items()
        if key
        not in {
            "deleted_event_id",
            "storage_paths",
            "storage_namespace",
            "already_purged",
        }
    }


def _result_from_receipt(
    receipt: EventPurgeReceipt,
    storage_paths: tuple[str, ...] | None = None,
) -> PurgeResult:
    counts = receipt.deletion_counts or {}
    return PurgeResult(
        deleted_event_id=receipt.event_id,
        deleted_revision_count=int(counts.get("deleted_revision_count", 0)),
        deleted_raw_message_count=int(counts.get("deleted_raw_message_count", 0)),
        deleted_assessment_run_count=int(counts.get("deleted_assessment_run_count", 0)),
        deleted_assessment_task_count=int(counts.get("deleted_assessment_task_count", 0)),
        deleted_collaboration_task_count=int(counts.get("deleted_collaboration_task_count", 0)),
        deleted_artifact_count=int(counts.get("deleted_artifact_count", 0)),
        deleted_publication_count=int(counts.get("deleted_publication_count", 0)),
        deleted_override_count=int(counts.get("deleted_override_count", 0)),
        deleted_task_event_count=int(counts.get("deleted_task_event_count", 0)),
        storage_paths=(
            storage_paths
            if storage_paths is not None
            else tuple(str(path) for path in (receipt.storage_paths or ()))
        ),
        storage_namespace=receipt.storage_namespace,
        already_purged=True,
    )


def _require_receipt_namespace(receipt: EventPurgeReceipt) -> str:
    raw_namespace = receipt.storage_namespace
    if not isinstance(raw_namespace, str) or not raw_namespace.strip():
        raise PurgeNamespaceUnavailableError(
            "event purge receipt storage namespace is unavailable; manual remediation required"
        )
    try:
        namespace = _normalize_storage_namespace(raw_namespace)
    except ValueError as error:
        raise PurgeNamespaceUnavailableError(
            "event purge receipt storage namespace is unavailable; manual remediation required"
        ) from error
    if namespace == LEGACY_STORAGE_NAMESPACE:
        raise PurgeNamespaceUnavailableError(
            "event purge receipt storage namespace is unavailable; manual remediation required"
        )
    return namespace


def _normalize_storage_namespace(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("storage namespace must not be empty")
    namespace = value.strip()
    if len(namespace) > 128:
        raise ValueError("storage namespace is too long")
    if any(character.isspace() for character in namespace):
        raise ValueError("storage namespace must not contain whitespace")
    return namespace
