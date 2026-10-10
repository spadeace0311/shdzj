from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.events.domain import EventKind
from app.events.service import EventService
from app.operations.domain import OperationsScenario, TriggerMode
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.priority import OperationsPriorityGuard
from app.operations.repository import OperationsRepository
from app.operations.scenario import ScenarioGenerator
from app.operations.schemas import (
    DrillTriggerRequest,
    ManualTestTriggerRequest,
    OperationsOverviewRecord,
    OperationsRunRecord,
    OperationsScheduleRecord,
    OperationsSettingsRecord,
    OperationsSettingsUpdateRequest,
    OperationsTriggerResult,
)
from app.regions.service import RegionContextResolver


class RealEventPriorityActive(RuntimeError):
    def __init__(self) -> None:
        super().__init__("real_event_priority_active")


class OperationsService:
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100
    OVERVIEW_RUN_LIMIT = 5
    ACTIVE_RUN_STATES = ("queued", "running", "paused")
    MANUAL_PRIORITY = 100
    SCHEDULED_PRIORITY = 10

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        *,
        event_service: EventService | None = None,
        repository: OperationsRepository | None = None,
        scenario_generator: ScenarioGenerator | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._event_service = event_service or EventService(session_factory)
        self._repository = repository or OperationsRepository()
        self._scenario_generator = scenario_generator or ScenarioGenerator()
        self._priority_guard = OperationsPriorityGuard(repository=self._repository)
        self._region_resolver = RegionContextResolver(session_factory)
        self._now = now

    async def trigger_manual_test(
        self,
        request: ManualTestTriggerRequest,
        actor: object,
    ) -> OperationsTriggerResult:
        return await self._trigger_manual(
            TriggerMode.MANUAL_TEST,
            EventKind.TEST,
            request.idempotency_key,
            actor,
        )

    async def trigger_drill(
        self,
        request: DrillTriggerRequest,
        actor: object,
    ) -> OperationsTriggerResult:
        return await self._trigger_manual(
            TriggerMode.DRILL,
            EventKind.DRILL,
            request.idempotency_key,
            actor,
        )

    async def trigger_scheduled_test(
        self,
        schedule_id: object,
        actor: object,
    ) -> OperationsTriggerResult:
        schedule_uuid = _coerce_uuid(schedule_id, "schedule_id")
        observed_at = _as_utc(self._now())
        actor_name = _actor_name(actor)
        key = f"scheduled:{schedule_uuid}"

        async with self._session_factory() as session:
            async with session.begin():
                schedule = await session.get(
                    OperationsTestSchedule,
                    schedule_uuid,
                    with_for_update=True,
                )
                if schedule is None:
                    raise LookupError("operations_schedule_not_found")

                if await self._priority_guard.has_active_real_event(
                    session,
                    observed_at,
                ):
                    return _blocked_trigger_result(
                        key=key,
                        trigger_mode=TriggerMode.SCHEDULED_TEST.value,
                        event_kind=EventKind.TEST.value,
                        operation_state="deferred",
                    )

                settings = await self._repository.get_settings(session)
                scenario = await self._scheduled_scenario(
                    session,
                    schedule,
                    settings,
                    observed_at,
                )

        region_context = await self._region_resolver.resolve(
            scenario.longitude,
            scenario.latitude,
        )
        event = self._scenario_generator.scenario_to_event(scenario)
        outcome = await self._event_service.ingest_collected(
            raw_payload={
                "idempotency_key": key,
                "mode": TriggerMode.SCHEDULED_TEST.value,
            },
            event=event,
            provider="api",
            lane="http",
            received_at=observed_at,
            response_input=None,
            region_context=region_context,
            trigger_reason="live",
        )
        run = await self._persist_run_transaction(
            event_id=outcome.event_id,
            revision_id=outcome.revision_id,
            schedule_id=schedule_uuid,
            trigger_mode=TriggerMode.SCHEDULED_TEST.value,
            operation_state="queued",
            priority=self.SCHEDULED_PRIORITY,
            idempotency_key=key,
            triggered_by=actor_name,
            observed_at=observed_at,
        )
        return OperationsTriggerResult(
            event_id=run.event_id,
            run_id=run.id,
            revision_id=outcome.revision_id,
            trigger_mode=TriggerMode.SCHEDULED_TEST.value,
            operation_state=run.operation_state,
            outcome="triggered",
            idempotency_key=key,
            event_kind=EventKind.TEST.value,
            triggered_assessment=outcome.triggered_assessment,
        )

    async def list_schedules(
        self,
        limit: int = DEFAULT_LIMIT,
    ) -> list[OperationsScheduleRecord]:
        normalized_limit = _normalize_limit(limit)
        async with self._session_factory() as session:
            async with session.begin():
                return await self._list_schedules(session, normalized_limit)

    async def list_runs(
        self,
        limit: int = DEFAULT_LIMIT,
    ) -> list[OperationsRunRecord]:
        normalized_limit = _normalize_limit(limit)
        async with self._session_factory() as session:
            async with session.begin():
                return await self._list_runs(session, normalized_limit)

    async def get_settings(self) -> OperationsSettingsRecord:
        async with self._session_factory() as session:
            async with session.begin():
                settings = await self._repository.get_settings(session)
                return _settings_record(settings)

    async def get_overview(
        self,
        observed_at: datetime,
    ) -> OperationsOverviewRecord:
        observed_at = _as_utc(observed_at)
        async with self._session_factory() as session:
            async with session.begin():
                settings = await self._repository.get_settings(session)
                local_date = observed_at.astimezone(ZoneInfo(settings.timezone)).date()
                today = await session.scalar(
                    select(OperationsTestSchedule)
                    .where(OperationsTestSchedule.local_date == local_date)
                    .order_by(
                        OperationsTestSchedule.created_at.desc(),
                        OperationsTestSchedule.id.desc(),
                    )
                    .limit(1)
                )
                next_schedule = await session.scalar(
                    select(OperationsTestSchedule)
                    .where(OperationsTestSchedule.scheduled_at > observed_at)
                    .order_by(
                        OperationsTestSchedule.scheduled_at.asc(),
                        OperationsTestSchedule.id.asc(),
                    )
                    .limit(1)
                )
                active_run_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(OperationsRun)
                        .where(OperationsRun.operation_state.in_(self.ACTIVE_RUN_STATES))
                    )
                    or 0
                )
                recent_runs = await self._list_runs(
                    session,
                    self.OVERVIEW_RUN_LIMIT,
                )
                real_event_priority_active = await self._priority_guard.has_active_real_event(
                    session,
                    observed_at,
                )

        return OperationsOverviewRecord(
            observed_at=observed_at,
            real_event_priority_active=real_event_priority_active,
            settings=_settings_record(settings),
            next_schedule=(_schedule_record(next_schedule) if next_schedule is not None else None),
            today_schedule=(_schedule_record(today) if today is not None else None),
            recent_runs=recent_runs,
            active_run_count=active_run_count,
        )

    async def update_settings(
        self,
        request: OperationsSettingsUpdateRequest,
        actor: object,
        expected_version: int,
    ) -> OperationsSettingsRecord:
        patch = request.model_dump(
            exclude={"expected_version"},
            exclude_unset=True,
            exclude_none=True,
        )
        async with self._session_factory() as session:
            async with session.begin():
                settings = await self._repository.update_settings(
                    session,
                    _actor_name(actor),
                    int(expected_version),
                    patch,
                    observed_at=_as_utc(self._now()),
                )
                return _settings_record(settings)

    async def _trigger_manual(
        self,
        trigger_mode: TriggerMode,
        event_kind: EventKind,
        supplied_key: str | None,
        actor: object,
    ) -> OperationsTriggerResult:
        key = self._normalize_idempotency_key(supplied_key)
        observed_at = _as_utc(self._now())
        actor_name = _actor_name(actor)
        seed = self._derive_seed(key)
        source_event_id = self._derive_source_event_id(key, event_kind)

        scenario, blocked = await self._prepare_manual_scenario(
            trigger_mode=trigger_mode,
            event_kind=event_kind,
            key=key,
            observed_at=observed_at,
            seed=seed,
            source_event_id=source_event_id,
        )
        if blocked is not None:
            return blocked
        assert scenario is not None

        region_context = await self._region_resolver.resolve(
            scenario.longitude,
            scenario.latitude,
        )
        event = self._scenario_generator.scenario_to_event(scenario)
        if event_kind is EventKind.DRILL:
            event = replace(event, kind=EventKind.DRILL)
        outcome = await self._event_service.ingest_collected(
            raw_payload={
                "idempotency_key": key,
                "mode": trigger_mode.value,
            },
            event=event,
            provider="api",
            lane="http",
            received_at=observed_at,
            response_input=None,
            region_context=region_context,
            trigger_reason="live",
        )
        run = await self._persist_run_transaction(
            event_id=outcome.event_id,
            revision_id=outcome.revision_id,
            schedule_id=None,
            trigger_mode=trigger_mode.value,
            operation_state="queued",
            priority=self.MANUAL_PRIORITY,
            idempotency_key=key,
            triggered_by=actor_name,
            observed_at=observed_at,
        )
        return OperationsTriggerResult(
            event_id=run.event_id,
            run_id=run.id,
            revision_id=outcome.revision_id,
            trigger_mode=trigger_mode.value,
            operation_state=run.operation_state,
            outcome="triggered",
            idempotency_key=key,
            event_kind=event_kind.value,
            triggered_assessment=outcome.triggered_assessment,
        )

    async def _prepare_manual_scenario(
        self,
        *,
        trigger_mode: TriggerMode,
        event_kind: EventKind,
        key: str,
        observed_at: datetime,
        seed: int,
        source_event_id: str,
    ) -> tuple[OperationsScenario | None, OperationsTriggerResult | None]:
        async with self._session_factory() as session:
            async with session.begin():
                settings = await self._repository.get_settings(session)
                if await self._priority_guard.has_active_real_event(
                    session,
                    observed_at,
                ):
                    if trigger_mode is TriggerMode.MANUAL_TEST:
                        raise RealEventPriorityActive()
                    state = (
                        "canceled" if settings.drill_conflict_policy == "terminate" else "deferred"
                    )
                    return None, _blocked_trigger_result(
                        key=key,
                        trigger_mode=trigger_mode.value,
                        event_kind=event_kind.value,
                        operation_state=state,
                    )

                scenario = await self._scenario_generator.generate_test(
                    session,
                    settings,
                    observed_at=observed_at,
                    seed=seed,
                )
                scenario = replace(
                    scenario,
                    source_event_id=source_event_id,
                    random_seed=seed,
                )
                return scenario, None

    async def _scheduled_scenario(
        self,
        session: AsyncSession,
        schedule: OperationsTestSchedule,
        settings: OperationsSettings,
        observed_at: datetime,
    ) -> OperationsScenario:
        scenario = self._scenario_from_schedule(schedule)
        if scenario is None:
            scenario = await self._scenario_generator.generate_test(
                session,
                settings,
                observed_at=observed_at,
                seed=schedule.random_seed,
            )
        return replace(
            scenario,
            source_event_id=(f"operations-test:scheduled:{schedule.id.hex}"),
            random_seed=schedule.random_seed,
        )

    @staticmethod
    def _scenario_from_schedule(
        schedule: OperationsTestSchedule,
    ) -> OperationsScenario | None:
        value = schedule.scenario
        if not isinstance(value, dict):
            return None
        try:
            return OperationsScenario(
                origin_time=_require_aware_datetime(
                    value["origin_time"],
                    "scenario.origin_time",
                ),
                longitude=Decimal(str(value["longitude"])),
                latitude=Decimal(str(value["latitude"])),
                depth_km=Decimal(str(value["depth_km"])),
                magnitude=Decimal(str(value["magnitude"])),
                place=str(value["place"]),
                source_event_id=str(value.get("source_event_id") or ""),
                random_seed=int(value["random_seed"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None

    async def _persist_run_transaction(
        self,
        *,
        event_id: object,
        revision_id: object,
        schedule_id: object | None,
        trigger_mode: str,
        operation_state: str,
        priority: int,
        idempotency_key: str,
        triggered_by: str,
        observed_at: datetime,
    ) -> OperationsRun:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._persist_run(
                    session,
                    event_id=event_id,
                    revision_id=revision_id,
                    schedule_id=schedule_id,
                    trigger_mode=trigger_mode,
                    operation_state=operation_state,
                    priority=priority,
                    idempotency_key=idempotency_key,
                    triggered_by=triggered_by,
                    observed_at=observed_at,
                )

    async def _persist_run(
        self,
        session: AsyncSession,
        *,
        event_id: object,
        revision_id: object,
        schedule_id: object | None,
        trigger_mode: str,
        operation_state: str,
        priority: int,
        idempotency_key: str,
        triggered_by: str,
        observed_at: datetime,
    ) -> OperationsRun:
        event_uuid = _coerce_uuid(event_id, "event_id")
        schedule_uuid = (
            _coerce_uuid(schedule_id, "schedule_id") if schedule_id is not None else None
        )
        run_id = uuid.uuid4()
        insert_values = {
            "id": run_id,
            "event_id": event_uuid,
            "schedule_id": schedule_uuid,
            "trigger_mode": trigger_mode,
            "operation_state": operation_state,
            "priority": priority,
            "idempotency_key": idempotency_key,
            "triggered_by": triggered_by,
            "resume_required": False,
            "detail": {
                "revision_id": str(revision_id),
                "reason": "operations_trigger",
            },
            "created_at": observed_at,
            "updated_at": observed_at,
        }
        await session.execute(
            pg_insert(OperationsRun)
            .values(**insert_values)
            .on_conflict_do_nothing(index_elements=[OperationsRun.idempotency_key])
        )
        run = await session.scalar(
            select(OperationsRun)
            .where(OperationsRun.idempotency_key == idempotency_key)
            .order_by(OperationsRun.created_at.desc(), OperationsRun.id.desc())
            .limit(1)
        )
        if run is None:
            run = await session.scalar(
                select(OperationsRun)
                .where(OperationsRun.event_id == event_uuid)
                .order_by(OperationsRun.created_at.desc(), OperationsRun.id.desc())
                .limit(1)
            )
        if run is None:
            raise RuntimeError("operations run could not be persisted")
        if run.event_id != event_uuid or run.idempotency_key != idempotency_key:
            raise ValueError("idempotency_key conflicts with a different operation")

        session.add(
            OperationsAuditLog(
                action=f"operations.{trigger_mode}.triggered",
                actor=triggered_by,
                resource_type="operations_run",
                resource_id=str(run.id),
                before_state={},
                after_state={
                    "event_id": str(run.event_id),
                    "operation_state": run.operation_state,
                    "trigger_mode": run.trigger_mode,
                },
                detail={
                    "idempotency_key": idempotency_key,
                    "revision_id": str(revision_id),
                    "schedule_id": (str(schedule_uuid) if schedule_uuid is not None else None),
                },
                created_at=observed_at,
            )
        )
        await session.flush()
        return run

    async def _list_schedules(
        self,
        session: AsyncSession,
        limit: int,
    ) -> list[OperationsScheduleRecord]:
        rows = (
            await session.scalars(
                select(OperationsTestSchedule)
                .order_by(
                    OperationsTestSchedule.scheduled_at.desc(),
                    OperationsTestSchedule.id.desc(),
                )
                .limit(limit)
            )
        ).all()
        return [_schedule_record(row) for row in rows]

    async def _list_runs(
        self,
        session: AsyncSession,
        limit: int,
    ) -> list[OperationsRunRecord]:
        rows = (
            await session.scalars(
                select(OperationsRun)
                .order_by(
                    OperationsRun.created_at.desc(),
                    OperationsRun.id.desc(),
                )
                .limit(limit)
            )
        ).all()
        return [_run_record(row) for row in rows]

    @staticmethod
    def _normalize_idempotency_key(key: str | None) -> str:
        if key is None:
            return str(uuid.uuid4())
        if not isinstance(key, str) or not key.strip():
            raise ValueError("idempotency_key must not be empty")
        normalized = key.strip()
        if len(normalized) > 160:
            raise ValueError("idempotency_key is too long")
        return normalized

    @staticmethod
    def _derive_seed(key: str) -> int:
        digest = hashlib.sha256(key.encode("utf-8")).digest()[:8]
        return int.from_bytes(digest, "big", signed=True)

    @staticmethod
    def _derive_source_event_id(key: str, event_kind: EventKind) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        if event_kind is EventKind.DRILL:
            return f"operations-drill:{digest}"
        return f"operations-test:{digest}"


def _blocked_trigger_result(
    *,
    key: str,
    trigger_mode: str,
    event_kind: str,
    operation_state: str,
) -> OperationsTriggerResult:
    return OperationsTriggerResult(
        event_id=None,
        run_id=None,
        revision_id=None,
        trigger_mode=trigger_mode,
        operation_state=operation_state,
        outcome=operation_state,
        idempotency_key=key,
        event_kind=event_kind,
        triggered_assessment=False,
    )


def _schedule_record(schedule: OperationsTestSchedule) -> OperationsScheduleRecord:
    return OperationsScheduleRecord(
        id=schedule.id,
        local_date=schedule.local_date,
        window_start=schedule.window_start,
        window_end=schedule.window_end,
        scheduled_at=schedule.scheduled_at,
        state=schedule.state,
        scenario=schedule.scenario,
        random_seed=schedule.random_seed,
        event_id=schedule.event_id,
        failure_reason=schedule.failure_reason,
        triggered_at=schedule.triggered_at,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def _run_record(run: OperationsRun) -> OperationsRunRecord:
    return OperationsRunRecord(
        id=run.id,
        event_id=run.event_id,
        schedule_id=run.schedule_id,
        trigger_mode=run.trigger_mode,
        operation_state=run.operation_state,
        priority=run.priority,
        idempotency_key=run.idempotency_key,
        triggered_by=run.triggered_by,
        resume_required=run.resume_required,
        detail=run.detail,
        started_at=run.started_at,
        paused_at=run.paused_at,
        completed_at=run.completed_at,
        canceled_at=run.canceled_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _settings_record(settings: OperationsSettings) -> OperationsSettingsRecord:
    return OperationsSettingsRecord(
        row_version=settings.row_version,
        daily_test_enabled=settings.daily_test_enabled,
        timezone=settings.timezone,
        daily_window_start=settings.daily_window_start,
        daily_window_end=settings.daily_window_end,
        test_magnitude_min=settings.test_magnitude_min,
        test_magnitude_max=settings.test_magnitude_max,
        test_depth_min_km=settings.test_depth_min_km,
        test_depth_max_km=settings.test_depth_max_km,
        drill_conflict_policy=settings.drill_conflict_policy,
        updated_by=settings.updated_by,
        updated_at=settings.updated_at,
    )


def _normalize_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= OperationsService.MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {OperationsService.MAX_LIMIT}")
    return limit


def _actor_name(actor: object) -> str:
    username = getattr(actor, "username", actor)
    if not isinstance(username, str) or not username.strip():
        raise ValueError("actor must be a non-empty string")
    return username.strip()


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("observed_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)


def _require_aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value.astimezone(UTC)


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
