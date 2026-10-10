from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.models import EarthquakeEvent
from app.operations.models import (
    OperationsRun,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository


class OperationsWorkBlocked(RuntimeError):
    def __init__(self) -> None:
        super().__init__("operations_work_blocked")


@dataclass(frozen=True, slots=True)
class PriorityResult:
    deferred_schedules: int = 0
    canceled_test_runs: int = 0
    paused_drill_runs: int = 0
    canceled_drill_runs: int = 0


class OperationsPriorityGuard:
    PROTECTION_WINDOW = timedelta(days=7)
    PRIORITY_LOCK_KEY = "operations-priority"
    REAL_EVENT_TYPES = ("formal", "manual")
    TEST_EVENT_TYPES = ("test", "drill")
    ACTIVE_RUN_STATES = ("queued", "running")
    FROZEN_RUN_STATES = ("paused", "canceled")

    def __init__(
        self,
        repository: OperationsRepository | None = None,
    ) -> None:
        self._repository = repository or OperationsRepository()

    async def has_active_real_event(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> bool:
        normalized = _as_utc(observed_at)
        if not _supports_query_session(session):
            return False
        event_id = await session.scalar(
            select(EarthquakeEvent.id)
            .where(
                *_active_real_event_conditions(normalized),
            )
            .limit(1)
        )
        return event_id is not None

    async def apply_real_event_start(
        self,
        session: AsyncSession,
        event_id: object,
        observed_at: datetime,
    ) -> PriorityResult:
        normalized = _as_utc(observed_at)
        real_event_id = _coerce_uuid(event_id, "event_id")
        if not _supports_query_session(session):
            return PriorityResult()
        await _acquire_priority_lock(session)
        if not await self._is_active_real_event(
            session,
            real_event_id,
            normalized,
        ):
            return PriorityResult()

        settings = await self._repository.get_settings(session)
        local_date = normalized.astimezone(
            ZoneInfo(settings.timezone)
        ).date()
        schedules = (
            await session.scalars(
                select(OperationsTestSchedule)
                .where(
                    OperationsTestSchedule.local_date == local_date,
                    OperationsTestSchedule.state.in_(("scheduled", "deferred")),
                )
                .order_by(OperationsTestSchedule.id)
                .with_for_update()
            )
        ).all()
        for schedule in schedules:
            schedule.state = "deferred"
            schedule.updated_at = normalized

        rows = (
            await session.execute(
                select(OperationsRun, EarthquakeEvent.event_type)
                .join(
                    EarthquakeEvent,
                    EarthquakeEvent.id == OperationsRun.event_id,
                )
                .where(
                    EarthquakeEvent.event_type.in_(self.TEST_EVENT_TYPES),
                    OperationsRun.operation_state.in_(self.ACTIVE_RUN_STATES),
                )
                .order_by(OperationsRun.id)
                .with_for_update(of=OperationsRun)
            )
        ).all()
        canceled_test_runs = 0
        paused_drill_runs = 0
        canceled_drill_runs = 0
        for run, event_type in rows:
            detail = dict(run.detail or {})
            detail["reason"] = "real_event_priority"
            run.detail = detail
            run.updated_at = normalized
            if event_type == "test":
                run.operation_state = "canceled"
                run.canceled_at = normalized
                run.resume_required = False
                canceled_test_runs += 1
                continue

            if settings.drill_conflict_policy == "terminate":
                run.operation_state = "canceled"
                run.canceled_at = normalized
                run.resume_required = False
                canceled_drill_runs += 1
            else:
                run.operation_state = "paused"
                run.paused_at = normalized
                run.resume_required = True
                paused_drill_runs += 1

        await session.flush()
        return PriorityResult(
            deferred_schedules=len(schedules),
            canceled_test_runs=canceled_test_runs,
            paused_drill_runs=paused_drill_runs,
            canceled_drill_runs=canceled_drill_runs,
        )

    async def ensure_event_work_allowed(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> None:
        event_uuid = _coerce_uuid(event_id, "event_id")
        if not _supports_query_session(session):
            return
        operation_state = await session.scalar(
            select(OperationsRun.operation_state)
            .join(
                EarthquakeEvent,
                EarthquakeEvent.id == OperationsRun.event_id,
            )
            .where(
                OperationsRun.event_id == event_uuid,
                EarthquakeEvent.event_type.in_(self.TEST_EVENT_TYPES),
            )
            .order_by(OperationsRun.id)
            .limit(1)
            .with_for_update(of=OperationsRun)
        )
        if operation_state in self.FROZEN_RUN_STATES:
            raise OperationsWorkBlocked()

    async def release_deferred_runs(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> int:
        normalized = _as_utc(observed_at)
        if not _supports_query_session(session):
            return 0
        await _acquire_priority_lock(session)
        if await self.has_active_real_event(session, normalized):
            return 0

        result = await session.execute(
            update(OperationsTestSchedule)
            .where(
                OperationsTestSchedule.state == "deferred",
                OperationsTestSchedule.scheduled_at <= normalized,
            )
            .values(
                state="scheduled",
                updated_at=normalized,
            )
        )
        await session.flush()
        return int(result.rowcount or 0)

    async def _is_active_real_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        observed_at: datetime,
    ) -> bool:
        found = await session.scalar(
            select(EarthquakeEvent.id)
            .where(
                EarthquakeEvent.id == event_id,
                *_active_real_event_conditions(observed_at),
            )
            .limit(1)
        )
        return found is not None


def _active_real_event_conditions(observed_at: datetime):
    return (
        EarthquakeEvent.current_revision_id.is_not(None),
        EarthquakeEvent.event_type.in_(
            OperationsPriorityGuard.REAL_EVENT_TYPES
        ),
        EarthquakeEvent.origin_time <= observed_at,
        EarthquakeEvent.origin_time
        >= observed_at - OperationsPriorityGuard.PROTECTION_WINDOW,
    )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("observed_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _supports_query_session(session: object) -> bool:
    return callable(getattr(session, "scalar", None))


async def _acquire_priority_lock(session: AsyncSession) -> None:
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:lock_key, 0)"
            ")"
        ),
        {"lock_key": OperationsPriorityGuard.PRIORITY_LOCK_KEY},
    )
