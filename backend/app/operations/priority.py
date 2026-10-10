from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.models import EarthquakeEvent, EarthquakeRevision
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository
from app.operations.scenario import OperationsConfigurationError


class OperationsWorkBlocked(RuntimeError):
    def __init__(self) -> None:
        super().__init__("operations_work_blocked")


class RealEventPriorityActive(RuntimeError):
    def __init__(self) -> None:
        super().__init__("real_event_priority_active")


@dataclass(frozen=True, slots=True)
class PriorityResult:
    deferred_schedules: int = 0
    canceled_test_runs: int = 0
    paused_drill_runs: int = 0
    canceled_drill_runs: int = 0


class OperationsPriorityGuard:
    ACTOR = "operations-priority"
    PROTECTION_WINDOW = timedelta(days=7)
    PRIORITY_LOCK_KEY = "operations-priority"
    REAL_EVENT_TYPES = ("formal", "manual", "correction")
    TEST_EVENT_TYPES = ("test", "drill")
    ACTIVE_RUN_STATES = ("queued", "running")
    FROZEN_RUN_STATES = ("paused", "canceled", "failed")

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
            .join(
                EarthquakeRevision,
                EarthquakeRevision.id == EarthquakeEvent.current_revision_id,
            )
            .where(*_active_real_event_conditions(normalized))
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

        try:
            settings = await self._repository.get_settings(session)
        except OperationsConfigurationError:
            settings = None
        local_date = None
        if settings is not None:
            local_date = normalized.astimezone(
                ZoneInfo(settings.timezone)
            ).date()
        schedules = (
            await session.scalars(
                select(OperationsTestSchedule)
                .where(
                    OperationsTestSchedule.state.in_(("scheduled", "deferred")),
                )
                .order_by(OperationsTestSchedule.id)
                .with_for_update()
            )
        ).all()
        if local_date is not None:
            schedules = [
                schedule
                for schedule in schedules
                if schedule.local_date == local_date
            ]
        for schedule in schedules:
            before = {
                "state": schedule.state,
                "updated_at": schedule.updated_at.isoformat(),
            }
            schedule.state = "deferred"
            schedule.updated_at = normalized
            session.add(
                _priority_audit(
                    action="operations.priority.schedule_deferred",
                    resource_type="operations_test_schedule",
                    resource_id=str(schedule.id),
                    before_state=before,
                    after_state={
                        "state": schedule.state,
                        "updated_at": schedule.updated_at.isoformat(),
                    },
                    real_event_id=real_event_id,
                    observed_at=normalized,
                )
            )

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
            before = _run_audit_state(run)
            detail = dict(run.detail or {})
            detail["reason"] = "real_event_priority"
            run.detail = detail
            run.updated_at = normalized
            if event_type == "test":
                action = "operations.priority.test_canceled"
                run.operation_state = "canceled"
                run.canceled_at = normalized
                run.resume_required = False
                canceled_test_runs += 1
            elif (
                settings is not None
                and settings.drill_conflict_policy == "terminate"
            ):
                action = "operations.priority.drill_canceled"
                run.operation_state = "canceled"
                run.canceled_at = normalized
                run.resume_required = False
                canceled_drill_runs += 1
            else:
                action = "operations.priority.drill_paused"
                run.operation_state = "paused"
                run.paused_at = normalized
                run.resume_required = True
                paused_drill_runs += 1
            session.add(
                _priority_audit(
                    action=action,
                    resource_type="operations_run",
                    resource_id=str(run.id),
                    before_state=before,
                    after_state=_run_audit_state(run),
                    real_event_id=real_event_id,
                    observed_at=normalized,
                    extra_detail={
                        "reason": detail.get("reason"),
                        "event_type": event_type,
                    },
                )
            )

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

        schedules = (
            await session.scalars(
                select(OperationsTestSchedule)
                .where(
                    OperationsTestSchedule.state == "deferred",
                    OperationsTestSchedule.scheduled_at <= normalized,
                )
                .order_by(OperationsTestSchedule.id)
                .with_for_update()
            )
        ).all()
        for schedule in schedules:
            before = {
                "state": schedule.state,
                "updated_at": schedule.updated_at.isoformat(),
            }
            schedule.state = "scheduled"
            schedule.updated_at = normalized
            session.add(
                _priority_audit(
                    action="operations.priority.schedule_released",
                    resource_type="operations_test_schedule",
                    resource_id=str(schedule.id),
                    before_state=before,
                    after_state={
                        "state": schedule.state,
                        "updated_at": schedule.updated_at.isoformat(),
                    },
                    real_event_id=None,
                    observed_at=normalized,
                )
            )
        await session.flush()
        return len(schedules)

    async def _is_active_real_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        observed_at: datetime,
    ) -> bool:
        found = await session.scalar(
            select(EarthquakeEvent.id)
            .join(
                EarthquakeRevision,
                EarthquakeRevision.id == EarthquakeEvent.current_revision_id,
            )
            .where(
                EarthquakeEvent.id == event_id,
                *_active_real_event_conditions(observed_at),
            )
            .limit(1)
        )
        return found is not None


def _active_real_event_conditions(observed_at: datetime):
    activated_at = func.coalesce(
        EarthquakeEvent.real_event_activated_at,
        EarthquakeRevision.ingested_at,
        EarthquakeEvent.created_at,
    )
    return (
        EarthquakeEvent.current_revision_id.is_not(None),
        EarthquakeEvent.event_type.in_(
            OperationsPriorityGuard.REAL_EVENT_TYPES
        ),
        EarthquakeEvent.real_event_closed_at.is_(None),
        activated_at <= observed_at,
        activated_at
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


def _run_audit_state(run: OperationsRun) -> dict:
    return {
        "operation_state": run.operation_state,
        "resume_required": bool(run.resume_required),
        "detail": dict(run.detail or {}),
        "paused_at": run.paused_at.isoformat() if run.paused_at else None,
        "canceled_at": run.canceled_at.isoformat() if run.canceled_at else None,
    }


def _priority_audit(
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    before_state: dict,
    after_state: dict,
    real_event_id: uuid.UUID | None,
    observed_at: datetime,
    extra_detail: dict | None = None,
) -> OperationsAuditLog:
    detail = {
        "reason": "real_event_priority",
        "real_event_id": str(real_event_id) if real_event_id else None,
    }
    if extra_detail:
        detail.update(extra_detail)
    return OperationsAuditLog(
        action=action,
        actor=OperationsPriorityGuard.ACTOR,
        resource_type=resource_type,
        resource_id=resource_id,
        before_state=before_state,
        after_state=after_state,
        detail=detail,
        created_at=observed_at,
    )
