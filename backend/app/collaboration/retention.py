from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ProductionRun, ProductionTask
from app.assessment.models import AssessmentRun
from app.collaboration.models import (
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.event_object_cleanup import enqueue_object_cleanup_intent
from app.events.models import EarthquakeEvent, EventLifecycleOutbox
from app.operations.retention_clock import retention_cutoff


logger = logging.getLogger(__name__)

ACTIVE_TASK_STATUSES = {"pending", "in_progress", "pending_review"}
ACTIVE_OUTBOX_STATUSES = {"pending", "processing"}
ACTIVE_ASSESSMENT_STATUSES = {"pending", "running"}


@dataclass(frozen=True, slots=True)
class CollaborationRetentionResult:
    test_deleted: int = 0
    drill_deleted: int = 0
    protected_active_dependencies: int = 0
    batches: int = 0


class CollaborationRetentionService:
    def __init__(self, *, batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._batch_size = batch_size

    async def purge_due(
        self,
        session: AsyncSession,
        observed_at: datetime,
        *,
        storage_namespace: str | None = None,
    ) -> CollaborationRetentionResult:
        observed_at = _as_utc(observed_at)
        result = CollaborationRetentionResult()
        for event_type in ("test", "drill"):
            cutoff = self._cutoff_for(event_type, observed_at)
            deleted, protected, batches = await self._purge_event_type(
                session,
                event_type=event_type,
                cutoff=cutoff,
                storage_namespace=storage_namespace,
            )
            result = CollaborationRetentionResult(
                test_deleted=(
                    result.test_deleted + deleted
                    if event_type == "test"
                    else result.test_deleted
                ),
                drill_deleted=(
                    result.drill_deleted + deleted
                    if event_type == "drill"
                    else result.drill_deleted
                ),
                protected_active_dependencies=(
                    result.protected_active_dependencies + protected
                ),
                batches=result.batches + batches,
            )
        logger.info(
            "collaboration retention purge complete test=%s drill=%s "
            "protected_active_dependencies=%s batches=%s",
            result.test_deleted,
            result.drill_deleted,
            result.protected_active_dependencies,
            result.batches,
        )
        return result

    async def _purge_event_type(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        cutoff: datetime,
        storage_namespace: str | None,
    ) -> tuple[int, int, int]:
        deleted = 0
        protected = 0
        batches = 0
        cursor_id = None
        while True:
            statement = (
                select(EarthquakeEvent)
                .where(
                    EarthquakeEvent.event_type == event_type,
                    EarthquakeEvent.origin_time <= cutoff,
                )
                .order_by(EarthquakeEvent.id)
                .limit(self._batch_size)
                .with_for_update()
            )
            if cursor_id is not None:
                statement = statement.where(EarthquakeEvent.id > cursor_id)
            rows = (await session.scalars(statement)).all()
            if not rows:
                break
            cursor_id = rows[-1].id
            batches += 1
            for event in rows:
                if await self._has_active_dependencies(session, event.id):
                    protected += 1
                    continue
                manual_paths = await self._manual_storage_paths(session, event.id)
                await session.delete(event)
                for version_id, storage_path in manual_paths:
                    if storage_namespace is not None:
                        await enqueue_object_cleanup_intent(
                            session,
                            event_id=event.id,
                            source_kind="event_retention",
                            source_key=str(version_id),
                            storage_path=storage_path,
                            storage_namespace=storage_namespace,
                        )
                deleted += 1
            await session.flush()
        return deleted, protected, batches

    async def _has_active_dependencies(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> bool:
        tasks = (
            await session.scalars(
                select(WorkgroupTask)
            .where(
                WorkgroupTask.event_id == event_id,
            )
                .with_for_update()
            )
        )
        if any(task.status in ACTIVE_TASK_STATUSES for task in tasks):
            return True

        outboxes = (
            await session.scalars(
                select(EventLifecycleOutbox)
                .where(EventLifecycleOutbox.event_id == event_id)
                .with_for_update()
            )
        )
        if any(row.status in ACTIVE_OUTBOX_STATUSES for row in outboxes):
            return True

        assessments = (
            await session.scalars(
                select(AssessmentRun)
                .where(AssessmentRun.event_id == event_id)
                .with_for_update()
            )
        )
        if any(row.status in ACTIVE_ASSESSMENT_STATUSES for row in assessments):
            return True

        production_runs = (
            await session.scalars(
                select(ProductionRun)
                .where(ProductionRun.event_id == event_id)
                .with_for_update()
            )
        )
        if any(row.status in {"pending", "running"} for row in production_runs):
            return True

        production_tasks = (
            await session.scalars(
                select(ProductionTask)
                .join(
                    ProductionRun,
                    ProductionRun.id == ProductionTask.production_run_id,
                )
                .where(ProductionRun.event_id == event_id)
                .with_for_update()
            )
        )
        return any(
            row.status in {"pending", "ready", "running"}
            for row in production_tasks
        )

    @staticmethod
    async def _manual_storage_paths(
        session: AsyncSession,
        event_id: object,
    ) -> list[tuple[object, str]]:
        rows = await session.execute(
            select(TaskDeliverableVersion.id, TaskDeliverableVersion.storage_key)
            .join(
                TaskDeliverable,
                TaskDeliverable.id == TaskDeliverableVersion.deliverable_id,
            )
            .join(
                WorkgroupTask,
                WorkgroupTask.id == TaskDeliverable.task_id,
            )
            .where(
                WorkgroupTask.event_id == event_id,
                TaskDeliverableVersion.storage_key.is_not(None),
            )
        )
        return list(rows.all())

    @staticmethod
    def _cutoff_for(event_type: str, observed_at: datetime) -> datetime:
        return retention_cutoff(event_type, observed_at)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
