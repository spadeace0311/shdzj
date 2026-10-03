from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.db import Base
from app.event_object_locks import (
    lock_artifact_object,
    lock_event_write,
    storage_path_reference_count,
    unreferenced_storage_paths,
)


CANDIDATE_DELETE_CLEANUP_SOURCE = "candidate_delete"
ARTIFACT_RETENTION_CLEANUP_SOURCE = "retention"


class CleanupIntentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class EventObjectCleanupIntent(Base):
    """Durable post-commit object deletion intent.

    The row intentionally has no event foreign key: the source event may be
    purged before the worker gets a chance to delete its private object.
    """

    __tablename__ = "event_object_cleanup_intents"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "source_key",
            "storage_path",
            name="uq_event_object_cleanup_source_path",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'skipped')",
            name="ck_event_object_cleanup_status",
        ),
        Index(
            "ix_event_object_cleanup_claim",
            "status",
            "lease_expires_at",
        ),
        Index("ix_event_object_cleanup_event_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CleanupIntentStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True, slots=True)
class ObjectCleanupIntent:
    event_id: uuid.UUID
    storage_paths: tuple[str, ...] = ()
    source_kind: str = ""
    source_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "storage_paths",
            tuple(sorted({path for path in self.storage_paths if path})),
        )


@dataclass(frozen=True, slots=True)
class ObjectCleanupResult:
    deleted_paths: tuple[str, ...] = ()
    retained_paths: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failed_paths


@dataclass(frozen=True, slots=True)
class CleanupProcessingResult:
    claimed_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0

    @property
    def succeeded(self) -> bool:
        return self.failed_count == 0


async def enqueue_object_cleanup_intent(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    source_kind: str,
    source_key: str,
    storage_path: str,
) -> EventObjectCleanupIntent:
    if not source_kind:
        raise ValueError("source_kind must not be empty")
    if not source_key:
        raise ValueError("source_key must not be empty")
    if not storage_path:
        raise ValueError("storage_path must not be empty")

    statement = (
        insert(EventObjectCleanupIntent)
        .values(
            event_id=event_id,
            source_kind=source_kind,
            source_key=source_key,
            storage_path=storage_path,
            status=CleanupIntentStatus.PENDING.value,
        )
        .on_conflict_do_nothing(
            constraint="uq_event_object_cleanup_source_path",
        )
        .returning(EventObjectCleanupIntent.id)
    )
    intent_id = await session.scalar(statement)
    if intent_id is None:
        intent_id = await session.scalar(
            select(EventObjectCleanupIntent.id).where(
                EventObjectCleanupIntent.source_kind == source_kind,
                EventObjectCleanupIntent.source_key == source_key,
                EventObjectCleanupIntent.storage_path == storage_path,
            )
        )
    if intent_id is None:
        raise RuntimeError("cleanup intent could not be persisted")
    intent = await session.get(EventObjectCleanupIntent, intent_id)
    if intent is None:
        raise RuntimeError("cleanup intent could not be loaded")
    return intent


async def process_cleanup_intents(
    session_factory: object,
    artifact_store: ArtifactStore,
    *,
    limit: int = 100,
    lease_seconds: float = 60.0,
    lease_owner: str | None = None,
    observed_at: datetime | None = None,
    source_kind: str | None = None,
    source_key: str | None = None,
) -> CleanupProcessingResult:
    """Claim and consume durable cleanup intents.

    Claiming is committed before file I/O. A worker crash therefore leaves a
    leased row that can be reclaimed after the lease expires. The actual
    reference check and unlink execute in the common advisory-lock order.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = _as_utc(observed_at or datetime.now(UTC), "observed_at")
    owner = lease_owner or f"cleanup-{uuid.uuid4().hex}"
    lease_until = now + timedelta(seconds=lease_seconds)

    claim_filters = [
        or_(
            EventObjectCleanupIntent.status == CleanupIntentStatus.PENDING.value,
            and_(
                EventObjectCleanupIntent.status == CleanupIntentStatus.PROCESSING.value,
                EventObjectCleanupIntent.lease_expires_at.is_not(None),
                EventObjectCleanupIntent.lease_expires_at <= now,
            ),
        )
    ]
    if source_kind is not None:
        claim_filters.append(EventObjectCleanupIntent.source_kind == source_kind)
    if source_key is not None:
        claim_filters.append(EventObjectCleanupIntent.source_key == source_key)

    async with session_factory() as session:  # type: ignore[operator]
        async with session.begin():
            intents = (
                await session.scalars(
                    select(EventObjectCleanupIntent)
                    .where(*claim_filters)
                    .order_by(
                        EventObjectCleanupIntent.created_at,
                        EventObjectCleanupIntent.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not intents:
                return CleanupProcessingResult()
            for intent in intents:
                intent.status = CleanupIntentStatus.PROCESSING.value
                intent.attempt_count += 1
                intent.last_error = None
                intent.lease_owner = owner
                intent.lease_expires_at = lease_until
                intent.updated_at = now
            claimed_ids = [intent.id for intent in intents]

    completed_count = 0
    skipped_count = 0
    failed_count = 0
    for intent_id in claimed_ids:
        async with session_factory() as session:  # type: ignore[operator]
            async with session.begin():
                intent = await session.scalar(
                    select(EventObjectCleanupIntent)
                    .where(EventObjectCleanupIntent.id == intent_id)
                    .with_for_update()
                )
                if (
                    intent is None
                    or intent.status != CleanupIntentStatus.PROCESSING.value
                    or intent.lease_owner != owner
                ):
                    continue

                await lock_event_write(session, intent.event_id)
                await lock_artifact_object(session, intent.storage_path)
                now = datetime.now(UTC)
                if (
                    await storage_path_reference_count(
                        session,
                        intent.storage_path,
                    )
                    > 0
                ):
                    intent.status = CleanupIntentStatus.SKIPPED.value
                    intent.completed_at = now
                    intent.last_error = None
                    intent.lease_owner = None
                    intent.lease_expires_at = None
                    intent.updated_at = now
                    skipped_count += 1
                    continue

                try:
                    artifact_store.delete_unreferenced(
                        _stored_file(artifact_store, intent.storage_path)
                    )
                except FileNotFoundError:
                    pass
                except Exception as error:
                    intent.status = CleanupIntentStatus.PENDING.value
                    intent.last_error = str(error)[:2000]
                    intent.lease_owner = None
                    intent.lease_expires_at = None
                    intent.updated_at = now
                    failed_count += 1
                    continue

                intent.status = CleanupIntentStatus.COMPLETED.value
                intent.completed_at = now
                intent.last_error = None
                intent.lease_owner = None
                intent.lease_expires_at = None
                intent.updated_at = now
                completed_count += 1

    return CleanupProcessingResult(
        claimed_count=len(claimed_ids),
        completed_count=completed_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
    )


async def cleanup_object_intents(
    session: AsyncSession,
    intents: tuple[ObjectCleanupIntent, ...] | list[ObjectCleanupIntent],
    artifact_store: ArtifactStore,
    *,
    exclusive_event_locks: bool = False,
) -> ObjectCleanupResult:
    normalized = tuple(intent for intent in intents if intent.storage_paths)
    if not normalized:
        return ObjectCleanupResult()

    event_ids = sorted({intent.event_id for intent in normalized}, key=str)
    candidate_paths = sorted({path for intent in normalized for path in intent.storage_paths})

    # Lock every event before any object so multi-event cleanup cannot deadlock
    # with writers that acquire event then object locks.
    for event_id in event_ids:
        await lock_event_write(
            session,
            event_id,
            exclusive=exclusive_event_locks,
        )
    for storage_path in candidate_paths:
        await lock_artifact_object(session, storage_path)

    unreferenced = set(await unreferenced_storage_paths(session, candidate_paths))
    retained = tuple(path for path in candidate_paths if path not in unreferenced)
    deleted: list[str] = []
    failed: list[str] = []
    for storage_path in candidate_paths:
        if storage_path not in unreferenced:
            continue
        try:
            artifact_store.delete_unreferenced(
                StoredArtifactFile(
                    file_name=Path(storage_path).name,
                    relative_path=storage_path,
                    managed_path=artifact_store.resolve(storage_path),
                    size_bytes=0,
                    checksum="",
                )
            )
        except FileNotFoundError:
            deleted.append(storage_path)
        except (OSError, ValueError):
            failed.append(storage_path)
        else:
            deleted.append(storage_path)
    return ObjectCleanupResult(
        deleted_paths=tuple(deleted),
        retained_paths=retained,
        failed_paths=tuple(failed),
    )


async def cleanup_event_objects(
    session: AsyncSession,
    event_id: uuid.UUID,
    storage_paths: tuple[str, ...] | list[str],
    artifact_store: ArtifactStore,
    *,
    exclusive_event_lock: bool = False,
) -> ObjectCleanupResult:
    return await cleanup_object_intents(
        session,
        (ObjectCleanupIntent(event_id, tuple(storage_paths)),),
        artifact_store,
        exclusive_event_locks=exclusive_event_lock,
    )


def _stored_file(
    artifact_store: ArtifactStore,
    relative_path: str,
) -> StoredArtifactFile:
    return StoredArtifactFile(
        file_name=Path(relative_path).name,
        relative_path=relative_path,
        managed_path=artifact_store.resolve(relative_path),
        size_bytes=0,
        checksum="",
    )


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)
