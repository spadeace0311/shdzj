from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select, update

from app.collaboration.models import CollaborationSettings
from app.db import SessionFactory, engine
from app.events.models import EarthquakeEvent, EventLifecycleOutbox, RawMessage
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository
from app.operations.scheduler import DailyTestScheduler
from app.operations.schemas import OperationsTriggerResult
from app.operations.service import OperationsService
from app.regions.models import RegionBoundary


SOURCE = "operations-scheduler-test"
OBSERVED_AT = datetime(2040, 1, 15, 10, 0, tzinfo=UTC)
SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
async def _clean_scheduler_data():
    await engine.dispose()
    await _cleanup_data()
    yield
    await _cleanup_data()
    await engine.dispose()


async def _cleanup_data() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(OperationsRun))
            await session.execute(delete(OperationsTestSchedule))
            await session.execute(delete(OperationsAuditLog))
            await session.execute(delete(OperationsSettings))
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.source == SOURCE)
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.source == "operations"
                )
            )
            await session.execute(
                delete(RawMessage).where(
                    RawMessage.source.in_((SOURCE, "operations"))
                )
            )
            await session.execute(delete(EventLifecycleOutbox))


async def _ensure_settings(*, enabled: bool = True) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            settings = await OperationsRepository().get_settings(session)
            settings.daily_test_enabled = enabled


async def _ensure_boundary() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            existing = await session.scalar(
                select(RegionBoundary).where(
                    RegionBoundary.is_active.is_(True)
                ).limit(1)
            )
            if existing is not None:
                return
            session.add(
                RegionBoundary(
                    version=f"scheduler-test-boundary-{uuid.uuid4().hex}",
                    name="scheduler test boundary",
                    local_buffer_km=Decimal("50"),
                    geom=WKTElement(
                        "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                        "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                        srid=4326,
                    ),
                    maritime_geom=WKTElement(
                        "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, "
                        "122.2 31.8, 121.0 31.8, 121.0 30.8)))",
                        srid=4326,
                    ),
                    source_uri="test://operations-scheduler-boundary",
                    checksum="b" * 64,
                    is_active=True,
                )
            )
            await session.flush()


async def _ensure_collaboration_settings() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            existing = await session.scalar(
                select(CollaborationSettings.id).limit(1)
            )
            if existing is not None:
                return
            session.add(
                CollaborationSettings(
                    id=uuid.UUID(
                        "00000000-0000-0000-0000-000000000031"
                    ),
                    intensity_threshold=Decimal("2.0"),
                    row_version=1,
                )
            )
            await session.flush()


async def _seed_event() -> EarthquakeEvent:
    async with SessionFactory() as session:
        async with session.begin():
            event = EarthquakeEvent(
                id=uuid.uuid4(),
                source=SOURCE,
                canonical_source_id=f"{SOURCE}:{uuid.uuid4()}",
                event_type="test",
                origin_time=OBSERVED_AT,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.5"),
                place="scheduler fixture event",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="not_applicable",
            )
            session.add(event)
            await session.flush()
            return event


async def _seed_legacy_due_run() -> tuple[
    OperationsTestSchedule,
    EarthquakeEvent,
    OperationsRun,
]:
    async with SessionFactory() as session:
        async with session.begin():
            schedule = OperationsTestSchedule(
                local_date=OBSERVED_AT.astimezone(SHANGHAI).date(),
                window_start=OBSERVED_AT - timedelta(hours=2),
                window_end=OBSERVED_AT,
                scheduled_at=OBSERVED_AT - timedelta(minutes=5),
                state="due",
                random_seed=123,
            )
            session.add(schedule)
            await session.flush()

            event = EarthquakeEvent(
                id=uuid.uuid4(),
                source=SOURCE,
                canonical_source_id=(
                    f"operations:operations-test:scheduled:{schedule.id.hex}"
                ),
                event_type="test",
                origin_time=OBSERVED_AT,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.5"),
                place="legacy scheduler recovery event",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="not_applicable",
            )
            session.add(event)
            await session.flush()

            run = OperationsRun(
                event_id=event.id,
                schedule_id=schedule.id,
                trigger_mode="scheduled_test",
                operation_state="queued",
                priority=10,
                idempotency_key=f"scheduled:{schedule.id}",
                triggered_by="operations-scheduler",
                detail={"revision_id": str(uuid.uuid4())},
            )
            session.add(run)
            await session.flush()
            return schedule, event, run


async def _ensure_schedule(observed_at: datetime) -> OperationsTestSchedule:
    async with SessionFactory() as session:
        async with session.begin():
            return await DailyTestScheduler().ensure_schedule(session, observed_at)


def _trigger_result(
    *,
    event_id: uuid.UUID | None,
    operation_state: str = "queued",
    outcome: str = "triggered",
) -> OperationsTriggerResult:
    return OperationsTriggerResult(
        event_id=event_id,
        run_id=uuid.uuid4() if event_id is not None else None,
        revision_id=None,
        trigger_mode="scheduled_test",
        operation_state=operation_state,
        outcome=outcome,
        idempotency_key=f"scheduled:{uuid.uuid4()}",
        event_kind="test",
        triggered_assessment=False,
    )


class FakeTrigger:
    def __init__(self, responses: list[OperationsTriggerResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def __call__(
        self,
        schedule_id: object,
        actor: object,
    ) -> OperationsTriggerResult:
        self.calls.append((uuid.UUID(str(schedule_id)), str(actor)))
        if not self.responses:
            raise AssertionError("unexpected trigger call")
        return self.responses.pop(0)


async def test_ensure_schedule_is_unique_per_shanghai_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_settings()
    monkeypatch.setattr(secrets, "randbelow", lambda _upper: 600)

    async def initialize() -> uuid.UUID:
        async with SessionFactory() as session:
            async with session.begin():
                schedule = await DailyTestScheduler().ensure_schedule(
                    session,
                    OBSERVED_AT,
                )
                return schedule.id

    first_id, second_id = await asyncio.gather(initialize(), initialize())

    assert first_id == second_id
    async with SessionFactory() as session:
        count = await session.scalar(
            select(func.count()).select_from(OperationsTestSchedule)
        )
    assert count == 1


async def test_schedule_time_is_inside_configured_shanghai_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_settings()
    monkeypatch.setattr(secrets, "randbelow", lambda _upper: 600)

    schedule = await _ensure_schedule(OBSERVED_AT)
    local_date = OBSERVED_AT.astimezone(SHANGHAI).date()
    window_start = datetime.combine(
        local_date,
        time(2, 0),
        tzinfo=SHANGHAI,
    ).astimezone(UTC)
    window_end = datetime.combine(
        local_date,
        time(4, 0),
        tzinfo=SHANGHAI,
    ).astimezone(UTC)

    assert schedule.scheduled_at == window_start + timedelta(seconds=600)
    assert window_start <= schedule.scheduled_at < window_end


async def test_same_seed_is_stable_and_different_dates_can_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_settings()
    monkeypatch.setattr(secrets, "randbelow", lambda _upper: 600)

    first = await _ensure_schedule(OBSERVED_AT)
    repeated = await _ensure_schedule(OBSERVED_AT)
    next_day = await _ensure_schedule(OBSERVED_AT + timedelta(days=1))

    assert first.id == repeated.id
    assert first.scheduled_at == repeated.scheduled_at
    assert next_day.scheduled_at == first.scheduled_at + timedelta(days=1)


async def test_due_schedule_triggers_exactly_once() -> None:
    await _ensure_settings()
    event = await _seed_event()
    trigger = FakeTrigger([_trigger_result(event_id=event.id)])

    scheduler = DailyTestScheduler()
    first = await scheduler.dispatch_due(OBSERVED_AT, trigger)
    second = await scheduler.dispatch_due(OBSERVED_AT, trigger)

    assert first.created == 1
    assert first.triggered == 1
    assert second.created == 0
    assert second.triggered == 0
    assert len(trigger.calls) == 1

    async with SessionFactory() as session:
        schedule = await session.scalar(
            select(OperationsTestSchedule)
            .where(OperationsTestSchedule.local_date == OBSERVED_AT.astimezone(SHANGHAI).date())
        )
    assert schedule is not None
    assert schedule.state == "triggered"
    assert schedule.event_id == event.id
    assert schedule.triggered_at is not None


async def test_real_event_deferral_remains_eligible_after_window() -> None:
    await _ensure_settings()
    event = await _seed_event()
    schedule = await _ensure_schedule(OBSERVED_AT)
    trigger = FakeTrigger(
        [
            _trigger_result(
                event_id=None,
                operation_state="deferred",
                outcome="deferred",
            ),
            _trigger_result(event_id=event.id),
        ]
    )

    scheduler = DailyTestScheduler()
    first = await scheduler.dispatch_due(OBSERVED_AT, trigger)
    second = await scheduler.dispatch_due(OBSERVED_AT, trigger)

    assert first.deferred == 1
    assert first.triggered == 0
    assert second.deferred == 0
    assert second.triggered == 1

    async with SessionFactory() as session:
        stored = await session.get(OperationsTestSchedule, schedule.id)
    assert stored is not None
    assert stored.state == "triggered"
    assert stored.event_id == event.id


async def test_disabled_scheduling_marks_plan_skipped() -> None:
    await _ensure_settings(enabled=False)
    await _ensure_schedule(OBSERVED_AT)
    trigger = FakeTrigger([])

    result = await DailyTestScheduler().dispatch_due(OBSERVED_AT, trigger)

    assert trigger.calls == []
    async with SessionFactory() as session:
        schedule = await session.scalar(
            select(OperationsTestSchedule)
            .where(OperationsTestSchedule.local_date == OBSERVED_AT.astimezone(SHANGHAI).date())
        )
    assert schedule is not None
    assert schedule.state == "skipped"
    assert result.triggered == 0


async def test_missing_event_id_fails_with_audit_and_does_not_retry() -> None:
    await _ensure_settings()
    await _ensure_schedule(OBSERVED_AT)
    trigger = FakeTrigger(
        [
            _trigger_result(
                event_id=None,
                operation_state="queued",
                outcome="triggered",
            )
        ]
    )

    scheduler = DailyTestScheduler()
    first = await scheduler.dispatch_due(OBSERVED_AT, trigger)
    second = await scheduler.dispatch_due(OBSERVED_AT, trigger)

    assert first.failed == 1
    assert second.failed == 0
    assert len(trigger.calls) == 1

    async with SessionFactory() as session:
        schedule = await session.scalar(
            select(OperationsTestSchedule)
            .where(OperationsTestSchedule.local_date == OBSERVED_AT.astimezone(SHANGHAI).date())
        )
        audit = await session.scalar(
            select(OperationsAuditLog)
            .where(OperationsAuditLog.action == "operations.schedule.failed")
            .order_by(OperationsAuditLog.created_at.desc(), OperationsAuditLog.id.desc())
            .limit(1)
        )

    assert schedule is not None
    assert schedule.state == "failed"
    assert schedule.failure_reason == "trigger_result_missing_event_id"
    assert audit is not None
    assert audit.resource_type == "operations_test_schedule"
    assert audit.resource_id == str(schedule.id)
    assert audit.after_state["failure_reason"] == "trigger_result_missing_event_id"


async def test_no_active_boundary_fails_without_infinite_retry() -> None:
    await _ensure_settings()
    await _ensure_schedule(OBSERVED_AT)

    active_before: list[uuid.UUID] = []
    try:
        async with SessionFactory() as session:
            async with session.begin():
                active_before = (
                    await session.scalars(
                        select(RegionBoundary.id).where(
                            RegionBoundary.is_active.is_(True)
                        )
                    )
                ).all()
                await session.execute(
                    update(RegionBoundary).values(is_active=False)
                )

        service = OperationsService(SessionFactory, now=lambda: OBSERVED_AT)
        calls = 0

        async def service_trigger(
            schedule_id: object,
            actor: object,
        ) -> OperationsTriggerResult:
            nonlocal calls
            calls += 1
            return await service.trigger_scheduled_test(schedule_id, actor)

        scheduler = DailyTestScheduler()
        first = await scheduler.dispatch_due(OBSERVED_AT, service_trigger)
        second = await scheduler.dispatch_due(OBSERVED_AT, service_trigger)

        assert first.failed == 1
        assert second.failed == 0
        assert calls == 1

        async with SessionFactory() as session:
            schedule = await session.scalar(
                select(OperationsTestSchedule).where(
                    OperationsTestSchedule.local_date
                    == OBSERVED_AT.astimezone(SHANGHAI).date()
                )
            )
            audit = await session.scalar(
                select(OperationsAuditLog)
                .where(OperationsAuditLog.action == "operations.schedule.failed")
                .order_by(
                    OperationsAuditLog.created_at.desc(),
                    OperationsAuditLog.id.desc(),
                )
                .limit(1)
            )
        assert schedule is not None
        assert schedule.state == "failed"
        assert schedule.failure_reason
        assert "active_shanghai_boundary_required" in schedule.failure_reason
        assert audit is not None
        assert audit.resource_type == "operations_test_schedule"
        assert audit.resource_id == str(schedule.id)
        assert "active_shanghai_boundary_required" in audit.after_state["failure_reason"]
    finally:
        if active_before:
            async with SessionFactory() as session:
                async with session.begin():
                    await session.execute(
                        update(RegionBoundary)
                        .where(RegionBoundary.id.in_(tuple(active_before)))
                        .values(is_active=True)
                    )


async def test_due_legacy_run_recovers_schedule_without_second_run() -> None:
    await _ensure_settings()
    await _ensure_boundary()
    await _ensure_collaboration_settings()
    schedule, event, existing_run = await _seed_legacy_due_run()

    service = OperationsService(SessionFactory, now=lambda: OBSERVED_AT)

    async def service_trigger(
        schedule_id: object,
        actor: object,
    ) -> OperationsTriggerResult:
        return await service.trigger_scheduled_test(schedule_id, actor)

    result = await DailyTestScheduler().dispatch_due(
        OBSERVED_AT,
        service_trigger,
    )

    assert result.triggered == 1
    assert result.failed == 0

    async with SessionFactory() as session:
        stored_schedule = await session.get(
            OperationsTestSchedule,
            schedule.id,
        )
        stored_event = await session.get(EarthquakeEvent, event.id)
        stored_run = await session.get(OperationsRun, existing_run.id)
        run_count = await session.scalar(
            select(func.count()).select_from(OperationsRun)
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.event_type == "test")
        )

    assert stored_schedule is not None
    assert stored_schedule.state == "triggered"
    assert stored_schedule.event_id == event.id
    assert stored_event is not None
    assert stored_run is not None
    assert stored_run.idempotency_key == f"scheduled:{schedule.id}"
    assert run_count == 1
    assert event_count == 1
