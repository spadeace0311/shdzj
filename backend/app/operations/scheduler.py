from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.operations.models import OperationsTestSchedule
from app.operations.repository import OperationsRepository
from app.operations.schemas import OperationsTriggerResult


@dataclass(frozen=True, slots=True)
class OperationsSchedulerResult:
    created: int = 0
    due: int = 0
    deferred: int = 0
    triggered: int = 0
    failed: int = 0


class DailyTestScheduler:
    ACTOR = "operations-scheduler"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        *,
        repository: OperationsRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or OperationsRepository()

    async def ensure_schedule(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> OperationsTestSchedule:
        schedule, _created = await self._ensure_schedule(session, observed_at)
        return schedule

    async def dispatch_due(
        self,
        observed_at: datetime,
        trigger: Callable[[object, str], Awaitable[OperationsTriggerResult]],
    ) -> OperationsSchedulerResult:
        observed_at = _as_utc(observed_at)
        async with self._session_factory() as session:
            async with session.begin():
                _schedule, created = await self._ensure_schedule(
                    session,
                    observed_at,
                )
                settings = await self._repository.get_settings(session)
                trigger_ids, due_count = await self._claim_due_schedules(
                    session,
                    observed_at,
                    enabled=settings.daily_test_enabled,
                )

        triggered = 0
        deferred = 0
        failed = 0
        for schedule_id in trigger_ids:
            outcome = await self._trigger_one(
                schedule_id,
                observed_at,
                trigger,
            )
            if outcome == "triggered":
                triggered += 1
            elif outcome == "deferred":
                deferred += 1
            elif outcome == "failed":
                failed += 1

        return OperationsSchedulerResult(
            created=1 if created else 0,
            due=due_count,
            deferred=deferred,
            triggered=triggered,
            failed=failed,
        )

    async def _ensure_schedule(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> tuple[OperationsTestSchedule, bool]:
        observed_at = _as_utc(observed_at)
        settings = await self._repository.get_settings(session)
        zone = ZoneInfo(settings.timezone)
        local_now = observed_at.astimezone(zone)
        local_date = local_now.date()
        window_start = datetime.combine(
            local_date,
            settings.daily_window_start,
            tzinfo=zone,
        )
        window_end = datetime.combine(
            local_date,
            settings.daily_window_end,
            tzinfo=zone,
        )
        window_seconds = int((window_end - window_start).total_seconds())
        if window_seconds <= 0:
            raise ValueError("daily_window_start must be before daily_window_end")

        random_seed = secrets.randbelow(window_seconds)
        scheduled_at = window_start.astimezone(UTC) + timedelta(seconds=random_seed)
        insert_result = await session.execute(
            pg_insert(OperationsTestSchedule)
            .values(
                id=uuid.uuid4(),
                local_date=local_date,
                window_start=window_start.astimezone(UTC),
                window_end=window_end.astimezone(UTC),
                scheduled_at=scheduled_at,
                state="scheduled",
                random_seed=random_seed,
                created_at=observed_at,
                updated_at=observed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[OperationsTestSchedule.local_date]
            )
        )
        created = insert_result.rowcount == 1

        schedule = await session.scalar(
            select(OperationsTestSchedule)
            .where(OperationsTestSchedule.local_date == local_date)
            .order_by(
                OperationsTestSchedule.created_at.desc(),
                OperationsTestSchedule.id.desc(),
            )
            .limit(1)
        )
        if schedule is None:
            raise RuntimeError("operations test schedule could not be initialized")
        return schedule, created

    async def _claim_due_schedules(
        self,
        session: AsyncSession,
        observed_at: datetime,
        *,
        enabled: bool,
    ) -> tuple[list[uuid.UUID], int]:
        rows = (
            await session.scalars(
                select(OperationsTestSchedule)
                .where(
                    OperationsTestSchedule.state.in_(
                        ("scheduled", "deferred", "due")
                    ),
                    OperationsTestSchedule.scheduled_at <= observed_at,
                )
                .order_by(
                    OperationsTestSchedule.scheduled_at,
                    OperationsTestSchedule.id,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()

        trigger_ids: list[uuid.UUID] = []
        for schedule in rows:
            if enabled:
                if schedule.state != "due":
                    schedule.state = "due"
                    schedule.updated_at = observed_at
                trigger_ids.append(schedule.id)
            else:
                schedule.state = "skipped"
                schedule.updated_at = observed_at
        await session.flush()
        return trigger_ids, len(rows)

    async def _trigger_one(
        self,
        schedule_id: uuid.UUID,
        observed_at: datetime,
        trigger: Callable[[object, str], Awaitable[OperationsTriggerResult]],
    ) -> str:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:lock_key, 0)"
                        ")"
                    ),
                    {
                        "lock_key": (
                            f"operations-scheduler:{schedule_id}"
                        )
                    },
                )
                schedule = await session.get(OperationsTestSchedule, schedule_id)
                if schedule is None or schedule.state != "due":
                    return "skipped"

                try:
                    result = await trigger(schedule_id, self.ACTOR)
                except Exception as exc:
                    schedule = await _get_schedule_for_update(
                        session,
                        schedule_id,
                    )
                    if schedule is None or schedule.state != "due":
                        return "skipped"
                    schedule.state = "failed"
                    schedule.failure_reason = _safe_error_text(exc)
                    schedule.updated_at = observed_at
                    await session.flush()
                    return "failed"

                schedule = await _get_schedule_for_update(
                    session,
                    schedule_id,
                )
                if schedule is None or schedule.state != "due":
                    return "skipped"

                if _is_deferred(result):
                    schedule.state = "deferred"
                    schedule.updated_at = observed_at
                    await session.flush()
                    return "deferred"

                event_id = _coerce_event_id(result.event_id)
                if event_id is None:
                    schedule.state = "failed"
                    schedule.failure_reason = "trigger_result_missing_event_id"
                    schedule.updated_at = observed_at
                    await session.flush()
                    return "failed"

                schedule.state = "triggered"
                schedule.event_id = event_id
                schedule.triggered_at = observed_at
                schedule.updated_at = observed_at
                await session.flush()
                return "triggered"


async def _get_schedule_for_update(
    session: AsyncSession,
    schedule_id: uuid.UUID,
) -> OperationsTestSchedule | None:
    return await session.scalar(
        select(OperationsTestSchedule)
        .where(OperationsTestSchedule.id == schedule_id)
        .with_for_update()
    )


def _is_deferred(result: OperationsTriggerResult) -> bool:
    return (
        result.operation_state == "deferred"
        or result.outcome == "deferred"
    )


def _coerce_event_id(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("observed_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
