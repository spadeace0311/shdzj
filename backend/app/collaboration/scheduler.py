from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.collaboration.domain import TimelinessState
from app.collaboration.models import NotificationDelivery, WorkgroupTask
from app.collaboration.roster import RosterService
from app.events.models import EarthquakeEvent


TERMINAL_TASK_STATUSES = {"completed", "not_required", "failed"}
EXTERNAL_CHANNELS = ("wecom", "email", "phone")
ACTIVE_LIVE_EVENT_STATES = {
    "active",
    "formal_triggered",
    "correction_triggered",
    "assessment_triggered",
}


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    tasks_scanned: int = 0
    timeliness_updated: int = 0
    assigned_created: int = 0
    due_soon_created: int = 0
    overdue_marked: int = 0
    overdue_reminder_count: int = 0
    affected_event_ids: frozenset[uuid.UUID] = frozenset()


class DeadlineScheduler:
    def __init__(
        self,
        *,
        channels: tuple[str, ...] = ("in_app",),
        roster_service: RosterService | None = None,
    ) -> None:
        self._channels = tuple(dict.fromkeys(("in_app", *channels)))
        self._roster_service = roster_service or RosterService()

    async def run_once(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> SchedulerResult:
        observed_at = _as_utc(observed_at)
        tasks = await self._list_nonterminal_tasks(session)
        active_formal = await self._has_active_live_formal(session)
        result = SchedulerResult(tasks_scanned=len(tasks))
        counts = {
            "assigned": 0,
            "due_soon": 0,
            "overdue": 0,
        }
        affected_event_ids: set[uuid.UUID] = set()
        for task in tasks:
            event = await session.get(EarthquakeEvent, task.event_id)
            if event is None:
                continue

            timeliness_changed, newly_overdue = self._apply_timeliness(
                task,
                observed_at,
            )
            if timeliness_changed:
                result = _increment(result, "timeliness_updated")
                affected_event_ids.add(task.event_id)
            if newly_overdue:
                result = _increment(result, "overdue_marked")

            allowed_channels = self._allowed_channels(event, active_formal)
            existing = await self._existing_delivery_keys(session, task.id)
            recipients = (
                await self._roster_service.list_task_notification_recipients(
                    session,
                    task.event_id,
                    task.workgroup_code,
                )
            )
            created_counts = {
                "assigned": 0,
                "due_soon": 0,
                "overdue": 0,
            }
            created_total = 0
            for recipient_user_id in recipients:
                recipient_counts = await self._schedule_for_task(
                    session,
                    task,
                    recipient_user_id,
                    observed_at,
                    allowed_channels=allowed_channels,
                    existing=existing,
                )
                for field, value in recipient_counts.items():
                    created_counts[field] += value
                created_total += sum(recipient_counts.values())

            if task.status == "pending_review":
                authority = (
                    await self._roster_service.resolve_confirming_authority(
                        session,
                        task.event_id,
                        task.workgroup_code,
                    )
                )
                if (
                    authority is not None
                    and authority.user_id in recipients
                ):
                    created_total += await self._ensure_deliveries(
                        session,
                        task,
                        authority.user_id,
                        intent_type="status_changed",
                        dedupe_key=(
                            "confirmation:pending_review:"
                            f"{task.row_version}"
                        ),
                        observed_at=observed_at,
                        allowed_channels=allowed_channels,
                        existing=existing,
                    )

            for field, value in created_counts.items():
                counts[field] += value
            if timeliness_changed or created_total:
                affected_event_ids.add(task.event_id)

        return SchedulerResult(
            tasks_scanned=result.tasks_scanned,
            timeliness_updated=result.timeliness_updated,
            assigned_created=counts["assigned"],
            due_soon_created=counts["due_soon"],
            overdue_marked=result.overdue_marked,
            overdue_reminder_count=counts["overdue"],
            affected_event_ids=frozenset(affected_event_ids),
        )

    async def _list_nonterminal_tasks(
        self,
        session: AsyncSession,
    ) -> list[WorkgroupTask]:
        rows = await session.scalars(
            select(WorkgroupTask)
            .where(WorkgroupTask.status.not_in(TERMINAL_TASK_STATUSES))
            .order_by(WorkgroupTask.created_at, WorkgroupTask.id)
        )
        return list(rows)

    async def _has_active_live_formal(
        self,
        session: AsyncSession,
    ) -> bool:
        event_id = await session.scalar(
            select(EarthquakeEvent.id)
            .where(
                EarthquakeEvent.event_type == "formal",
                EarthquakeEvent.lifecycle_state.in_(ACTIVE_LIVE_EVENT_STATES),
            )
            .limit(1)
        )
        return event_id is not None

    async def _existing_delivery_keys(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> set[tuple[uuid.UUID, str, str]]:
        rows = await session.execute(
            select(
                NotificationDelivery.recipient_user_id,
                NotificationDelivery.channel,
                NotificationDelivery.dedupe_key,
            ).where(NotificationDelivery.task_id == task_id)
        )
        return {
            (row.recipient_user_id, row.channel, row.dedupe_key)
            for row in rows
        }

    async def _schedule_for_task(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        recipient_user_id: uuid.UUID,
        observed_at: datetime,
        *,
        allowed_channels: tuple[str, ...],
        existing: set[tuple[uuid.UUID, str, str]],
    ) -> dict[str, int]:
        created = {
            "assigned": 0,
            "due_soon": 0,
            "overdue": 0,
        }
        if self._assignment_due(task, observed_at):
            created["assigned"] += await self._ensure_deliveries(
                session,
                task,
                recipient_user_id,
                intent_type="assigned",
                dedupe_key="assigned",
                observed_at=observed_at,
                allowed_channels=allowed_channels,
                existing=existing,
            )

        if task.due_at is None:
            return created

        if observed_at >= task.due_at:
            await self._ensure_deliveries(
                session,
                task,
                recipient_user_id,
                intent_type="overdue",
                dedupe_key="overdue:0",
                observed_at=observed_at,
                allowed_channels=allowed_channels,
                existing=existing,
            )
            elapsed_seconds = (observed_at - task.due_at).total_seconds()
            current_bucket = max(0, int(elapsed_seconds // 1800))
            for bucket in range(1, current_bucket + 1):
                created["overdue"] += await self._ensure_deliveries(
                    session,
                    task,
                    recipient_user_id,
                    intent_type="overdue",
                    dedupe_key=f"overdue:{bucket}",
                    observed_at=observed_at,
                    allowed_channels=allowed_channels,
                    existing=existing,
                )
        elif task.due_at - observed_at <= timedelta(minutes=15):
            created["due_soon"] += await self._ensure_deliveries(
                session,
                task,
                recipient_user_id,
                intent_type="due_soon",
                dedupe_key="due_soon",
                observed_at=observed_at,
                allowed_channels=allowed_channels,
                existing=existing,
            )
        return created

    async def _ensure_deliveries(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        recipient_user_id: uuid.UUID,
        *,
        intent_type: str,
        dedupe_key: str,
        observed_at: datetime,
        allowed_channels: tuple[str, ...],
        existing: set[tuple[uuid.UUID, str, str]],
    ) -> int:
        created = 0
        for channel in allowed_channels:
            key = (recipient_user_id, channel, dedupe_key)
            if key in existing:
                continue
            result = await session.execute(
                pg_insert(NotificationDelivery)
                .values(
                    id=uuid.uuid4(),
                    event_id=task.event_id,
                    task_id=task.id,
                    recipient_user_id=recipient_user_id,
                    intent_type=intent_type,
                    channel=channel,
                    dedupe_key=dedupe_key,
                    status="pending",
                    attempt_count=0,
                    available_at=observed_at,
                    created_at=observed_at,
                    updated_at=observed_at,
                )
                .on_conflict_do_nothing()
            )
            if result.rowcount == 1:
                existing.add(key)
                created += 1
        await session.flush()
        return 1 if created else 0

    @staticmethod
    def _assignment_due(task: WorkgroupTask, observed_at: datetime) -> bool:
        basis = task.activated_at or task.created_at
        return basis is not None and basis <= observed_at

    @staticmethod
    def _apply_timeliness(
        task: WorkgroupTask,
        observed_at: datetime,
    ) -> tuple[bool, bool]:
        if task.due_at is None:
            state = TimelinessState.ON_TIME
        elif observed_at >= task.due_at:
            state = TimelinessState.OVERDUE
        elif task.due_at - observed_at <= timedelta(minutes=15):
            state = TimelinessState.AT_RISK
        else:
            state = TimelinessState.ON_TIME
        changed = task.timeliness_state != state.value
        newly_overdue = changed and state is TimelinessState.OVERDUE
        if changed:
            task.timeliness_state = state.value
            task.updated_at = observed_at
        return changed, newly_overdue

    def _allowed_channels(
        self,
        event: EarthquakeEvent,
        active_formal: bool,
    ) -> tuple[str, ...]:
        if (
            active_formal
            and event.event_type in {"test", "drill"}
        ):
            return ("in_app",)
        return self._channels


def _increment(result: SchedulerResult, field: str) -> SchedulerResult:
    values = {
        "tasks_scanned": result.tasks_scanned,
        "timeliness_updated": result.timeliness_updated,
        "assigned_created": result.assigned_created,
        "due_soon_created": result.due_soon_created,
        "overdue_marked": result.overdue_marked,
        "overdue_reminder_count": result.overdue_reminder_count,
    }
    values[field] += 1
    return SchedulerResult(**values)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
