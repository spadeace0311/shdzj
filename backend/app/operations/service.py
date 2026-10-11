from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.artifacts.models import ProductionRun, ProductionTask
from app.assessment.models import AssessmentRun
from app.db import SessionFactory
from app.events.domain import EventKind
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    RawMessage,
)
from app.events.repository import EventIngestResult
from app.events.service import EventService
from app.operations.domain import OperationsScenario, TriggerMode
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.priority import OperationsPriorityGuard, RealEventPriorityActive
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


class _ExistingScheduledRunSignal(Exception):
    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__("existing scheduled run")
        self.run_id = run_id


class _ScheduledTriggerDeferredSignal(Exception):
    pass


class OperationsService:
    DEFAULT_LIMIT = 20
    MAX_LIMIT = 100
    OVERVIEW_RUN_LIMIT = 5
    ACTIVE_RUN_STATES = ("queued", "running", "paused")
    MANUAL_PRIORITY = 100
    SCHEDULED_PRIORITY = 10
    RESERVED_IDEMPOTENCY_PREFIXES = (
        "scheduled:",
        "operations-scheduler:",
        "resume:",
    )

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
        key = f"operations-scheduler:{schedule_uuid}"

        existing_run = await self._get_scheduled_run(schedule_uuid)
        if existing_run is not None:
            if (
                existing_run.trigger_mode != TriggerMode.SCHEDULED_TEST.value
                or existing_run.schedule_id != schedule_uuid
            ):
                raise ValueError("idempotency_key conflicts with a different operation")
            async with self._session_factory() as session:
                await self._validate_scheduled_run(
                    session,
                    existing_run,
                    schedule_uuid,
                )
            return _trigger_result(
                existing_run,
                revision_id=_revision_id_from_run(existing_run),
                trigger_mode=TriggerMode.SCHEDULED_TEST.value,
                event_kind=EventKind.TEST.value,
                triggered_assessment=False,
            )

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
            boundary_version=scenario.boundary_version,
        )
        event = self._scenario_generator.scenario_to_event(scenario)

        async def persist_scheduled_run(
            session: AsyncSession,
            result: EventIngestResult,
        ) -> None:
            await _acquire_priority_lock(session)
            await _acquire_scheduled_trigger_lock(session, schedule_uuid)
            real_event_active = await self._priority_guard.has_active_real_event(
                session,
                observed_at,
            )
            existing_scheduled_run = await self._find_scheduled_run(
                session,
                schedule_uuid,
                for_update=True,
            )
            if existing_scheduled_run is not None:
                await self._validate_scheduled_run(
                    session,
                    existing_scheduled_run,
                    schedule_uuid,
                )
                raise _ExistingScheduledRunSignal(existing_scheduled_run.id)
            if real_event_active:
                raise _ScheduledTriggerDeferredSignal()

            try:
                async with session.begin_nested():
                    await self._persist_run(
                        session,
                        event_id=result.event_id,
                        revision_id=result.revision_id,
                        schedule_id=schedule_uuid,
                        trigger_mode=TriggerMode.SCHEDULED_TEST.value,
                        operation_state="queued",
                        priority=self.SCHEDULED_PRIORITY,
                        idempotency_key=key,
                        triggered_by=actor_name,
                        observed_at=observed_at,
                    )
            except (ValueError, IntegrityError, RuntimeError):
                existing_scheduled_run = await self._find_scheduled_run(
                    session,
                    schedule_uuid,
                    for_update=True,
                )
                if existing_scheduled_run is None:
                    raise
                await self._validate_scheduled_run(
                    session,
                    existing_scheduled_run,
                    schedule_uuid,
                )
                raise _ExistingScheduledRunSignal(
                    existing_scheduled_run.id
                ) from None

        try:
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
                before_side_effects=persist_scheduled_run,
            )
        except _ExistingScheduledRunSignal as signal:
            return await self._scheduled_trigger_result(
                schedule_uuid,
                run_id=signal.run_id,
                triggered_assessment=False,
            )
        except _ScheduledTriggerDeferredSignal:
            return _blocked_trigger_result(
                key=key,
                trigger_mode=TriggerMode.SCHEDULED_TEST.value,
                event_kind=EventKind.TEST.value,
                operation_state="deferred",
            )

        return await self._scheduled_trigger_result(
            schedule_uuid,
            run_id=None,
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

    async def resume_drill(
        self,
        run_id: object,
        actor: object,
    ) -> OperationsTriggerResult:
        run_uuid = _coerce_uuid(run_id, "run_id")
        observed_at = _as_utc(self._now())
        actor_name = _actor_name(actor)
        resume_key = f"resume:{run_uuid}"

        existing_replacement = await self._get_run_by_idempotency_key(
            resume_key
        )

        async with self._session_factory() as session:
            async with session.begin():
                await _acquire_priority_lock(session)
                source = await session.get(
                    OperationsRun,
                    run_uuid,
                    with_for_update=True,
                )
                replacement = await self._find_run_by_idempotency_key(
                    session,
                    resume_key,
                    for_update=True,
                )
                if replacement is not None:
                    await self._validate_resume_replacement(
                        session,
                        source,
                        replacement,
                        run_uuid,
                    )
                    await self._link_resumed_source_in_session(
                        session,
                        source,
                        replacement.id,
                        run_uuid,
                        actor_name,
                        observed_at,
                    )
                    return _resume_trigger_result(
                        replacement,
                        run_uuid,
                    )

                if existing_replacement is not None:
                    raise ValueError(
                        "idempotency_key conflicts with a different operation"
                    )
                if source is None:
                    raise LookupError("operations_run_not_found")
                if source.trigger_mode != TriggerMode.DRILL.value:
                    raise ValueError("only paused drill runs can be resumed")
                if source.operation_state != "paused":
                    raise ValueError("only paused drill runs can be resumed")
                if not source.resume_required:
                    raise ValueError("drill run is not marked for resumption")
                if await self._priority_guard.has_active_real_event(
                    session,
                    observed_at,
                ):
                    raise RealEventPriorityActive()

        result = await self._trigger_manual(
            TriggerMode.DRILL,
            EventKind.DRILL,
            resume_key,
            actor,
            allow_reserved=True,
            resume_source_run_id=run_uuid,
        )
        if result.run_id is not None:
            await self._link_resumed_source(
                run_uuid,
                replacement_id=result.run_id,
                actor_name=actor_name,
                observed_at=observed_at,
            )
        return result.model_copy(update={"resumed_from_run_id": run_uuid})

    async def _link_resumed_source(
        self,
        source_run_id: uuid.UUID,
        *,
        replacement_id: uuid.UUID,
        actor_name: str,
        observed_at: datetime,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await _acquire_priority_lock(session)
                source = await session.get(
                    OperationsRun,
                    source_run_id,
                    with_for_update=True,
                )
                replacement = await session.get(
                    OperationsRun,
                    replacement_id,
                    with_for_update=True,
                )
                if replacement is None:
                    raise ValueError(
                        "idempotency_key conflicts with a different operation"
                    )
                await self._validate_resume_replacement(
                    session,
                    source,
                    replacement,
                    source_run_id,
                )
                await self._link_resumed_source_in_session(
                    session,
                    source,
                    replacement.id,
                    source_run_id,
                    actor_name,
                    observed_at,
                )

    async def _validate_resume_replacement(
        self,
        session: AsyncSession,
        source: OperationsRun | None,
        replacement: OperationsRun,
        source_run_id: uuid.UUID,
    ) -> None:
        if replacement.trigger_mode != TriggerMode.DRILL.value:
            raise ValueError(
                "idempotency_key conflicts with a different operation"
            )
        replacement_event = await session.get(
            EarthquakeEvent,
            replacement.event_id,
            with_for_update=True,
        )
        if (
            replacement_event is None
            or replacement_event.event_type != EventKind.DRILL.value
        ):
            raise ValueError(
                "idempotency_key conflicts with a different operation"
            )

        replacement_detail = dict(replacement.detail or {})
        replacement_source = replacement_detail.get(
            "resumed_from_source_run_id"
        )
        if (
            replacement_source is not None
            and replacement_source != str(source_run_id)
        ):
            raise ValueError(
                "idempotency_key conflicts with a different operation"
            )

        source_link = None
        if source is not None:
            if source.trigger_mode != TriggerMode.DRILL.value:
                raise ValueError(
                    "idempotency_key conflicts with a different operation"
                )
            source_detail = dict(source.detail or {})
            source_link = source_detail.get("resumed_by_run_id")
            if source_link is not None and source_link != str(
                replacement.id
            ):
                raise ValueError(
                    "idempotency_key conflicts with a different operation"
                )

        if source is None:
            if replacement_source != str(source_run_id):
                raise ValueError(
                    "idempotency_key conflicts with a different operation"
                )
            return

        if source_link is None and replacement_source is None:
            raise ValueError(
                "idempotency_key conflicts with a different operation"
            )

    async def _link_resumed_source_in_session(
        self,
        session: AsyncSession,
        source: OperationsRun | None,
        replacement_id: uuid.UUID,
        source_run_id: uuid.UUID,
        actor_name: str,
        observed_at: datetime,
    ) -> None:
        replacement = await session.get(
            OperationsRun,
            replacement_id,
            with_for_update=True,
        )
        if replacement is None:
            raise ValueError(
                "idempotency_key conflicts with a different operation"
            )
        replacement_detail = dict(replacement.detail or {})
        replacement_link = str(source_run_id)
        if (
            replacement_detail.get("resumed_from_source_run_id")
            != replacement_link
        ):
            replacement_detail["resumed_from_source_run_id"] = (
                replacement_link
            )
            replacement.detail = replacement_detail
            replacement.updated_at = observed_at

        if source is None:
            return

        detail = dict(source.detail or {})
        source_replacement_link = str(replacement.id)
        if source.operation_state == "paused":
            if not source.resume_required:
                raise ValueError("drill run is not marked for resumption")
            before = _run_state(source)
            source.operation_state = "canceled"
            source.resume_required = False
            source.canceled_at = observed_at
            source.updated_at = observed_at
            detail["resumed_by_run_id"] = source_replacement_link
            source.detail = detail
            session.add(
                OperationsAuditLog(
                    action="operations.drill.resumed",
                    actor=actor_name,
                    resource_type="operations_run",
                    resource_id=str(source.id),
                    before_state=before,
                    after_state=_run_state(source),
                    detail={
                        "resumed_by_run_id": source_replacement_link,
                    },
                    created_at=observed_at,
                )
            )
        elif source.operation_state in {
            "canceled",
            "failed",
            "completed",
        }:
            if detail.get("resumed_by_run_id") != source_replacement_link:
                detail["resumed_by_run_id"] = source_replacement_link
                source.detail = detail
                source.updated_at = observed_at
        else:
            raise ValueError("only paused drill runs can be resumed")

    async def reconcile_runs(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> int:
        observed_at = _as_utc(observed_at)
        rows = (
            await session.scalars(
                select(OperationsRun)
                .join(
                    EarthquakeEvent,
                    EarthquakeEvent.id == OperationsRun.event_id,
                )
                .where(
                    EarthquakeEvent.event_type.in_(("test", "drill")),
                    OperationsRun.operation_state.in_(("queued", "running")),
                )
                .order_by(OperationsRun.id)
                .with_for_update(of=OperationsRun)
            )
        ).all()

        reconciled = 0
        for run in rows:
            assessment_statuses = (
                await session.scalars(
                    select(AssessmentRun.status)
                    .join(
                        EarthquakeEvent,
                        EarthquakeEvent.id == AssessmentRun.event_id,
                    )
                    .where(
                        AssessmentRun.event_id == run.event_id,
                        AssessmentRun.revision_id
                        == EarthquakeEvent.current_revision_id,
                    )
                )
            ).all()
            production_statuses = (
                await session.scalars(
                    select(ProductionRun.status)
                    .join(
                        EarthquakeEvent,
                        EarthquakeEvent.id == ProductionRun.event_id,
                    )
                    .where(
                        ProductionRun.event_id == run.event_id,
                        ProductionRun.revision_id
                        == EarthquakeEvent.current_revision_id,
                    )
                )
            ).all()
            task_statuses = (
                await session.scalars(
                    select(ProductionTask.status)
                    .join(
                        ProductionRun,
                        ProductionRun.id == ProductionTask.production_run_id,
                    )
                    .join(
                        EarthquakeEvent,
                        EarthquakeEvent.id == ProductionRun.event_id,
                    )
                    .where(
                        ProductionRun.event_id == run.event_id,
                        ProductionRun.revision_id
                        == EarthquakeEvent.current_revision_id,
                    )
                )
            ).all()

            statuses = list(assessment_statuses)
            statuses.extend(production_statuses)
            statuses.extend(task_statuses)
            if not statuses:
                continue

            next_state: str | None = None
            production_pending = False
            if any(
                status in {"pending", "running", "ready"}
                for status in statuses
            ):
                next_state = "running"
            elif any(
                status in {"failed", "canceled", "timed_out"}
                for status in statuses
            ):
                next_state = "failed"
            elif (
                assessment_statuses
                and not production_statuses
                and not task_statuses
            ):
                next_state = "running"
                production_pending = True
            elif all(
                status
                in {
                    "completed",
                    "partial",
                    "succeeded",
                    "degraded",
                }
                for status in statuses
            ):
                next_state = "completed"

            if next_state is None:
                continue

            state_changed = next_state != run.operation_state
            if not state_changed and not production_pending:
                continue
            if production_pending:
                existing_audit_id = await session.scalar(
                    select(OperationsAuditLog.id)
                    .where(
                        OperationsAuditLog.resource_type == "operations_run",
                        OperationsAuditLog.resource_id == str(run.id),
                        OperationsAuditLog.action == "operations.run.running",
                        OperationsAuditLog.detail["reason"].astext
                        == "production_pending",
                    )
                    .limit(1)
                )
                if existing_audit_id is not None:
                    continue

            before = _run_state(run)
            if state_changed:
                run.operation_state = next_state
                run.updated_at = observed_at
                if next_state == "running" and run.started_at is None:
                    run.started_at = observed_at
                elif next_state == "completed":
                    run.completed_at = observed_at
                elif next_state == "failed":
                    run.completed_at = observed_at
                    run.resume_required = False
            session.add(
                OperationsAuditLog(
                    action=f"operations.run.{next_state}",
                    actor="operations-worker",
                    resource_type="operations_run",
                    resource_id=str(run.id),
                    before_state=before,
                    after_state=_run_state(run),
                    detail={
                        "reconciliation": True,
                        "event_id": str(run.event_id),
                        **(
                            {"reason": "production_pending"}
                            if production_pending
                            else {}
                        ),
                    },
                    created_at=observed_at,
                )
            )
            reconciled += 1

        await session.flush()
        return reconciled

    async def _trigger_manual(
        self,
        trigger_mode: TriggerMode,
        event_kind: EventKind,
        supplied_key: str | None,
        actor: object,
        *,
        allow_reserved: bool = False,
        resume_source_run_id: uuid.UUID | None = None,
    ) -> OperationsTriggerResult:
        key = self._normalize_idempotency_key(
            supplied_key,
            allow_reserved=allow_reserved,
        )
        observed_at = _as_utc(self._now())
        actor_name = _actor_name(actor)
        seed = self._derive_seed(key)
        source_event_id = self._derive_source_event_id(key, event_kind)

        existing_run = await self._get_run_by_idempotency_key(key)
        if existing_run is not None:
            if existing_run.trigger_mode != trigger_mode.value:
                raise ValueError("idempotency_key conflicts with a different operation")
            return _trigger_result(
                existing_run,
                revision_id=_revision_id_from_run(existing_run),
                trigger_mode=trigger_mode.value,
                event_kind=event_kind.value,
                triggered_assessment=False,
            )

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
            boundary_version=scenario.boundary_version,
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
        return await self._finalize_trigger(
            event_id=outcome.event_id,
            revision_id=outcome.revision_id,
            trigger_mode=trigger_mode,
            event_kind=event_kind,
            priority=self.MANUAL_PRIORITY,
            idempotency_key=key,
            triggered_by=actor_name,
            observed_at=observed_at,
            triggered_assessment=outcome.triggered_assessment,
            event_created=outcome.event_created,
            resume_source_run_id=resume_source_run_id,
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
                boundary_version=str(value.get("boundary_version") or ""),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None

    async def _get_run_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OperationsRun | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._find_run_by_idempotency_key(
                    session,
                    idempotency_key,
                )

    async def _find_run_by_idempotency_key(
        self,
        session: AsyncSession,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> OperationsRun | None:
        statement = (
            select(OperationsRun)
            .where(OperationsRun.idempotency_key == idempotency_key)
            .order_by(
                OperationsRun.created_at.desc(),
                OperationsRun.id.desc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def _get_scheduled_run(
        self,
        schedule_uuid: uuid.UUID,
    ) -> OperationsRun | None:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._find_scheduled_run(
                    session,
                    schedule_uuid,
                )

    async def _find_scheduled_run(
        self,
        session: AsyncSession,
        schedule_uuid: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> OperationsRun | None:
        statement = (
            select(OperationsRun)
            .where(OperationsRun.schedule_id == schedule_uuid)
            .order_by(
                OperationsRun.created_at.desc(),
                OperationsRun.id.desc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def _validate_scheduled_run(
        self,
        session: AsyncSession,
        run: OperationsRun,
        schedule_uuid: uuid.UUID,
    ) -> None:
        if (
            run.trigger_mode != TriggerMode.SCHEDULED_TEST.value
            or run.schedule_id != schedule_uuid
        ):
            raise ValueError("idempotency_key conflicts with a different operation")
        event = await session.get(EarthquakeEvent, run.event_id)
        if event is None or event.event_type != EventKind.TEST.value:
            raise ValueError("idempotency_key conflicts with a different operation")

    async def _scheduled_trigger_result(
        self,
        schedule_uuid: uuid.UUID,
        *,
        run_id: uuid.UUID | None,
        triggered_assessment: bool,
    ) -> OperationsTriggerResult:
        async with self._session_factory() as session:
            async with session.begin():
                run = (
                    await session.get(OperationsRun, run_id)
                    if run_id is not None
                    else await self._find_scheduled_run(
                        session,
                        schedule_uuid,
                    )
                )
                if run is None:
                    raise RuntimeError("scheduled operations run could not be found")
                await self._validate_scheduled_run(
                    session,
                    run,
                    schedule_uuid,
                )
                return _trigger_result(
                    run,
                    revision_id=_revision_id_from_run(run),
                    trigger_mode=TriggerMode.SCHEDULED_TEST.value,
                    event_kind=EventKind.TEST.value,
                    triggered_assessment=triggered_assessment,
                )

    async def _finalize_trigger(
        self,
        *,
        event_id: object,
        revision_id: object,
        trigger_mode: TriggerMode,
        event_kind: EventKind,
        priority: int,
        idempotency_key: str,
        triggered_by: str,
        observed_at: datetime,
        triggered_assessment: bool,
        event_created: bool,
        resume_source_run_id: uuid.UUID | None = None,
    ) -> OperationsTriggerResult:
        event_uuid = _coerce_uuid(event_id, "event_id")
        blocked_state: str | None = None
        real_event_rejected = False
        conflict_error: Exception | None = None
        run: OperationsRun | None = None

        async with self._session_factory() as session:
            async with session.begin():
                await _acquire_priority_lock(session)
                if await self._priority_guard.has_active_real_event(
                    session,
                    observed_at,
                ):
                    if event_created:
                        await self._delete_rejected_event(session, event_uuid)
                    if trigger_mode is TriggerMode.MANUAL_TEST:
                        real_event_rejected = True
                    elif trigger_mode is TriggerMode.DRILL:
                        settings = await self._repository.get_settings(session)
                        blocked_state = (
                            "canceled"
                            if settings.drill_conflict_policy == "terminate"
                            else "deferred"
                        )
                    else:
                        blocked_state = "deferred"
                else:
                    try:
                        async with session.begin_nested():
                            run = await self._persist_run(
                                session,
                                event_id=event_uuid,
                                revision_id=revision_id,
                                schedule_id=None,
                                trigger_mode=trigger_mode.value,
                                operation_state="queued",
                                priority=priority,
                                idempotency_key=idempotency_key,
                                triggered_by=triggered_by,
                                observed_at=observed_at,
                                resume_source_run_id=resume_source_run_id,
                            )
                    except (ValueError, IntegrityError) as exc:
                        conflict_error = exc
                        if event_created:
                            await self._delete_rejected_event(session, event_uuid)

        if real_event_rejected:
            raise RealEventPriorityActive()
        if conflict_error is not None:
            raise conflict_error
        if blocked_state is not None:
            return _blocked_trigger_result(
                key=idempotency_key,
                trigger_mode=trigger_mode.value,
                event_kind=event_kind.value,
                operation_state=blocked_state,
            )
        assert run is not None
        return _trigger_result(
            run,
            revision_id=str(revision_id),
            trigger_mode=trigger_mode.value,
            event_kind=event_kind.value,
            triggered_assessment=triggered_assessment,
        )

    async def _delete_rejected_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> None:
        raw_ids = (
            await session.scalars(
                select(EarthquakeRevision.raw_message_id).where(
                    EarthquakeRevision.event_id == event_id
                )
            )
        ).all()
        event = await session.get(EarthquakeEvent, event_id)
        if event is not None:
            await session.delete(event)
            await session.flush()
        if raw_ids:
            await session.execute(delete(RawMessage).where(RawMessage.id.in_(raw_ids)))
        await session.flush()

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
        resume_source_run_id: uuid.UUID | None = None,
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
                **(
                    {"resumed_from_source_run_id": str(resume_source_run_id)}
                    if resume_source_run_id is not None
                    else {}
                ),
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
    def _normalize_idempotency_key(
        key: str | None,
        *,
        allow_reserved: bool = False,
    ) -> str:
        if key is None:
            return str(uuid.uuid4())
        if not isinstance(key, str) or not key.strip():
            raise ValueError("idempotency_key must not be empty")
        normalized = key.strip()
        if len(normalized) > 160:
            raise ValueError("idempotency_key is too long")
        if not allow_reserved and normalized.startswith(
            OperationsService.RESERVED_IDEMPOTENCY_PREFIXES
        ):
            raise ValueError("idempotency_key uses a reserved namespace")
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


async def _acquire_priority_lock(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": OperationsPriorityGuard.PRIORITY_LOCK_KEY},
    )


async def _acquire_scheduled_trigger_lock(
    session: AsyncSession,
    schedule_uuid: uuid.UUID,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"operations-service-trigger:{schedule_uuid}"},
    )


def _trigger_result(
    run: OperationsRun,
    *,
    revision_id: str | None,
    trigger_mode: str,
    event_kind: str,
    triggered_assessment: bool,
) -> OperationsTriggerResult:
    return OperationsTriggerResult(
        event_id=run.event_id,
        run_id=run.id,
        revision_id=revision_id,
        trigger_mode=trigger_mode,
        operation_state=run.operation_state,
        outcome="triggered",
        idempotency_key=run.idempotency_key,
        event_kind=event_kind,
        triggered_assessment=triggered_assessment,
    )


def _resume_trigger_result(
    replacement: OperationsRun,
    source_run_id: uuid.UUID,
) -> OperationsTriggerResult:
    return _trigger_result(
        replacement,
        revision_id=_revision_id_from_run(replacement),
        trigger_mode=TriggerMode.DRILL.value,
        event_kind=EventKind.DRILL.value,
        triggered_assessment=False,
    ).model_copy(update={"resumed_from_run_id": source_run_id})


def _revision_id_from_run(run: OperationsRun) -> str | None:
    detail = run.detail
    if not isinstance(detail, dict):
        return None
    revision_id = detail.get("revision_id")
    return str(revision_id) if revision_id is not None else None


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


def _run_state(run: OperationsRun) -> dict:
    return {
        "operation_state": run.operation_state,
        "resume_required": bool(run.resume_required),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "paused_at": run.paused_at.isoformat() if run.paused_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "canceled_at": run.canceled_at.isoformat() if run.canceled_at else None,
    }


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
