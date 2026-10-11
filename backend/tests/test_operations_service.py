from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.artifacts.models import ProductionRun, ProductionTask
from app.assessment.models import AssessmentRun
from app.collaboration.models import CollaborationSettings
from app.db import SessionFactory, engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.repository import EventRepository
from app.events.service import LifecycleIngestOutcome
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository
from app.operations.schemas import DrillTriggerRequest, ManualTestTriggerRequest
from app.operations.scenario import ScenarioGenerator
from app.operations.service import OperationsService, RealEventPriorityActive
from app.regions.models import RegionBoundary


SOURCE = "operations-service-test"
OBSERVED_AT = datetime(2040, 1, 15, 4, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def clean_operations_service_data():
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
            await session.execute(delete(EarthquakeEvent).where(EarthquakeEvent.source == SOURCE))
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.source == "operations")
            )
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.source == "manual-source")
            )
            await session.execute(delete(RawMessage).where(RawMessage.source == SOURCE))
            await session.execute(delete(RawMessage).where(RawMessage.source == "operations"))
            await session.execute(delete(RawMessage).where(RawMessage.source == "manual-source"))
            await session.execute(delete(EventLifecycleOutbox))


async def _ensure_boundary(session) -> RegionBoundary:
    existing = await session.scalar(
        select(RegionBoundary).where(RegionBoundary.is_active.is_(True)).limit(1)
    )
    if existing is not None:
        return existing
    boundary = RegionBoundary(
        version=f"service-test-boundary-{uuid.uuid4().hex}",
        name="service test boundary",
        local_buffer_km=Decimal("50"),
        geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, " "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        maritime_geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, " "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        source_uri="test://operations-service-boundary",
        checksum="a" * 64,
        is_active=True,
    )
    session.add(boundary)
    await session.flush()
    return boundary


async def _ensure_collaboration_settings(session) -> None:
    existing = await session.scalar(select(CollaborationSettings.id).limit(1))
    if existing is not None:
        return
    session.add(
        CollaborationSettings(
            id=uuid.UUID("00000000-0000-0000-0000-000000000021"),
            intensity_threshold=Decimal("2.0"),
            row_version=1,
        )
    )
    await session.flush()


async def _seed_real_event(
    session,
    *,
    event_type: str = "formal",
    origin_time: datetime = OBSERVED_AT,
) -> EarthquakeEvent:
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source=SOURCE,
        canonical_source_id=f"{SOURCE}:{uuid.uuid4()}",
        event_type=event_type,
        origin_time=origin_time,
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="operations service real event",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="formal_triggered" if event_type == "formal" else "not_applicable",
    )
    session.add(event)
    await session.flush()
    raw = RawMessage(
        id=uuid.uuid4(),
        source=SOURCE,
        source_message_id=uuid.uuid4().hex,
        message_kind=event_type,
        checksum=uuid.uuid4().hex,
        payload={"event_id": str(event.id)},
    )
    session.add(raw)
    await session.flush()
    revision = EarthquakeRevision(
        id=uuid.uuid4(),
        event_id=event.id,
        raw_message_id=raw.id,
        revision_no=1,
        revision_kind=event_type,
        origin_time=event.origin_time,
        longitude=event.longitude,
        latitude=event.latitude,
        depth_km=event.depth_km,
        magnitude=event.magnitude,
        place=event.place,
        is_current=True,
        ingested_at=origin_time,
    )
    session.add(revision)
    await session.flush()
    event.current_revision_id = revision.id
    await session.flush()
    return event


async def _seed_legacy_scheduled_run(
    session,
) -> tuple[OperationsTestSchedule, EarthquakeEvent, OperationsRun]:
    schedule = OperationsTestSchedule(
        local_date=OBSERVED_AT.date(),
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
        place="legacy scheduled operations event",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="not_applicable",
    )
    session.add(event)
    await session.flush()

    revision_id = uuid.uuid4()
    run = OperationsRun(
        event_id=event.id,
        schedule_id=schedule.id,
        trigger_mode="scheduled_test",
        operation_state="queued",
        priority=10,
        idempotency_key=f"scheduled:{schedule.id}",
        triggered_by="operations-scheduler",
        detail={"revision_id": str(revision_id)},
    )
    session.add(run)
    await session.flush()
    return schedule, event, run


def _service(**kwargs) -> OperationsService:
    return OperationsService(SessionFactory, now=lambda: OBSERVED_AT, **kwargs)


class FakeEventService:
    def __init__(self) -> None:
        self.calls: list[EventKind] = []

    async def ingest_collected(self, **kwargs) -> LifecycleIngestOutcome:
        event = kwargs["event"]
        self.calls.append(event.kind)
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(event.source_event_id)))
        return LifecycleIngestOutcome(
            event_id=event_id,
            revision_id=str(uuid.uuid4()),
            revision_no=1,
            event_kind=event.kind,
            lifecycle_state="not_applicable",
            is_current=True,
            is_new=True,
            triggered_assessment=True,
            institutional_level=None,
            service_level=None,
            t1_at=None,
        )


class SequencedPriorityGuard:
    def __init__(self) -> None:
        self.calls = 0

    async def has_active_real_event(self, session, observed_at) -> bool:
        del session, observed_at
        self.calls += 1
        return self.calls > 1


async def test_manual_test_uses_real_event_service_and_creates_shanghai_event() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    result = await _service().trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="manual-real-1"),
        "operator",
    )

    assert result.event_id is not None
    assert result.run_id is not None
    assert result.operation_state == "queued"
    assert result.outcome == "triggered"

    async with SessionFactory() as session:
        event = await session.get(EarthquakeEvent, result.event_id)
        run = await session.get(OperationsRun, result.run_id)
        audit = await session.scalar(
            select(OperationsAuditLog)
            .where(OperationsAuditLog.action == "operations.manual_test.triggered")
            .order_by(OperationsAuditLog.created_at.desc(), OperationsAuditLog.id.desc())
            .limit(1)
        )
        revision = await session.scalar(
            select(EarthquakeRevision)
            .where(EarthquakeRevision.event_id == result.event_id)
            .limit(1)
        )

    assert event is not None
    assert event.event_type == "test"
    assert Decimal("5.0") <= event.magnitude <= Decimal("5.9")
    assert run is not None
    assert run.event_id == event.id
    assert run.operation_state == "queued"
    assert run.trigger_mode == "manual_test"
    assert audit is not None
    assert revision is not None
    assert revision.inside_shanghai is True


async def test_run_reconciliation_uses_current_terminal_state() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    result = await _service().trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="reconcile-completed"),
        "operator",
    )
    assert result.run_id is not None

    async with SessionFactory() as session:
        async with session.begin():
            event = await session.get(EarthquakeEvent, result.event_id)
            revision_id = uuid.UUID(result.revision_id)
            outbox = await session.scalar(
                select(EventLifecycleOutbox)
                .where(EventLifecycleOutbox.event_id == event.id)
                .limit(1)
            )
            assert event is not None and outbox is not None
            assessment_id = uuid.uuid4()
            assessment = AssessmentRun(
                id=assessment_id,
                event_id=event.id,
                revision_id=revision_id,
                outbox_id=outbox.id,
                run_no=1,
                status="completed",
                priority=100,
                deadline_at=OBSERVED_AT + timedelta(minutes=5),
                snapshot={},
                report_ingested_at=OBSERVED_AT,
                deadline_basis_at=OBSERVED_AT,
            )
            session.add(assessment)
            await session.flush()
            production_id = uuid.uuid4()
            production = ProductionRun(
                id=production_id,
                assessment_run_id=assessment.id,
                event_id=event.id,
                revision_id=revision_id,
                revision_no=1,
                production_mode="test",
                launch_mode="assessment_child",
                status="completed",
                priority=100,
                deadline_basis_at=OBSERVED_AT,
                deadline_at=OBSERVED_AT + timedelta(minutes=5),
                deadline_kind="event_deadline",
                catalog_version="reconciliation-test",
                generation_seq=1,
                generation_scope="full",
                required_outputs=[],
                is_current=True,
                snapshot={},
            )
            session.add(production)
            await session.flush()
            session.add(
                ProductionTask(
                    production_run_id=production.id,
                    artifact_key="reconcile.test",
                    output_profile="test",
                    kind="automatic",
                    priority=100,
                    sequence=1,
                    status="succeeded",
                    deadline_at=OBSERVED_AT + timedelta(minutes=5),
                    attempt_count=1,
                    max_attempts=3,
                    result={"status": "complete"},
                )
            )

    async with SessionFactory() as session:
        async with session.begin():
            reconciled = await _service().reconcile_runs(
                session,
                OBSERVED_AT,
            )
    assert reconciled >= 0

    async with SessionFactory() as session:
        run = await session.get(OperationsRun, result.run_id)
    assert run is not None
    assert run.operation_state == "completed"


async def test_resume_drill_creates_a_new_authorized_operation() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    first = await _service().trigger_drill(
        DrillTriggerRequest(idempotency_key="resume-source"),
        "operator",
    )
    async with SessionFactory() as session:
        async with session.begin():
            run = await session.get(OperationsRun, first.run_id)
            assert run is not None
            run.operation_state = "paused"
            run.resume_required = True
            run.paused_at = OBSERVED_AT

    resumed = await _service().resume_drill(first.run_id, "superadmin")

    async with SessionFactory() as session:
        old_run = await session.get(OperationsRun, first.run_id)
        new_run = await session.get(OperationsRun, resumed.run_id)

    assert resumed.run_id is not None
    assert resumed.resumed_from_run_id == first.run_id
    assert resumed.run_id != first.run_id
    assert old_run is not None
    assert old_run.operation_state == "paused"
    assert old_run.resume_required is True
    assert new_run is not None
    assert new_run.operation_state == "queued"


async def test_drill_uses_real_event_service_and_marks_drill() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    result = await _service().trigger_drill(
        DrillTriggerRequest(idempotency_key="drill-real-1"),
        "operator",
    )

    async with SessionFactory() as session:
        event = await session.get(EarthquakeEvent, result.event_id)
        run = await session.get(OperationsRun, result.run_id)

    assert event is not None
    assert event.event_type == "drill"
    assert run is not None
    assert run.trigger_mode == "drill"
    assert run.operation_state == "queued"


async def test_idempotency_replay_returns_same_event_and_run() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    service = _service()
    first = await service.trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="replay-key"),
        "operator",
    )
    second = await service.trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="replay-key"),
        "operator",
    )
    different = await service.trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="different-key"),
        "operator",
    )

    assert first.event_id == second.event_id
    assert first.run_id == second.run_id
    assert first.idempotency_key == second.idempotency_key == "replay-key"
    assert different.event_id != first.event_id
    assert different.run_id != first.run_id

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.source == "operations")
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))

    assert event_count == 2
    assert run_count == 2


async def test_manual_test_during_real_event_raises_priority_error() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await _seed_real_event(session)
            await OperationsRepository().get_settings(session)

    fake_event_service = FakeEventService()
    service = _service(event_service=fake_event_service)

    with pytest.raises(RealEventPriorityActive):
        await service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key="blocked-test"),
            "operator",
        )

    assert fake_event_service.calls == []


@pytest.mark.parametrize(
    ("policy", "expected_state"),
    [("pause", "deferred"), ("terminate", "canceled")],
)
async def test_drill_during_real_event_does_not_create_event(
    policy: str,
    expected_state: str,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await _seed_real_event(session)
            settings = await OperationsRepository().get_settings(session)
            settings.drill_conflict_policy = policy

    fake_event_service = FakeEventService()
    result = await _service(event_service=fake_event_service).trigger_drill(
        DrillTriggerRequest(idempotency_key=f"blocked-drill-{policy}"),
        "operator",
    )

    assert result.operation_state == expected_state
    assert result.outcome == expected_state
    assert result.event_id is None
    assert result.run_id is None
    assert fake_event_service.calls == []

    async with SessionFactory() as session:
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))

    assert run_count == 0


async def test_retry_repairs_missing_run_after_event_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    service = _service()
    original = service._persist_run
    failed = False

    async def flaky_persist(session, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise SQLAlchemyError("simulated run insert failure")
        return await original(session, **kwargs)

    monkeypatch.setattr(service, "_persist_run", flaky_persist)

    with pytest.raises(SQLAlchemyError):
        await service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key="repair-key"),
            "operator",
        )

    monkeypatch.setattr(service, "_persist_run", original)
    result = await service.trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="repair-key"),
        "operator",
    )

    assert result.event_id is not None
    assert result.run_id is not None

    async with SessionFactory() as session:
        run = await session.get(OperationsRun, result.run_id)
        event = await session.get(EarthquakeEvent, result.event_id)

    assert run is not None
    assert event is not None
    assert run.event_id == event.id


async def test_scenario_seed_and_source_event_id_are_derived_from_key() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await OperationsRepository().get_settings(session)

    generator = ScenarioGenerator()
    service = _service()
    key = " deterministic key with spaces "
    seed = service._derive_seed(service._normalize_idempotency_key(key))
    source_event_id = service._derive_source_event_id(
        service._normalize_idempotency_key(key),
        EventKind.TEST,
    )

    async with SessionFactory() as session:
        settings = await OperationsRepository().get_settings(session)
        first = await generator.generate_test(
            session,
            settings,
            observed_at=OBSERVED_AT,
            seed=seed,
        )
        second = await generator.generate_test(
            session,
            settings,
            observed_at=OBSERVED_AT,
            seed=seed,
        )

    assert first == second
    assert source_event_id.startswith("operations-test:")
    assert seed == int.from_bytes(
        __import__("hashlib")
        .sha256(service._normalize_idempotency_key(key).encode("utf-8"))
        .digest()[:8],
        "big",
        signed=True,
    )


async def test_same_key_across_manual_test_and_drill_conflicts_before_event_ingest() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    service = _service()
    first = await service.trigger_manual_test(
        ManualTestTriggerRequest(idempotency_key="shared-cross-mode-key"),
        "operator",
    )

    with pytest.raises(ValueError, match="idempotency_key conflicts"):
        await service.trigger_drill(
            DrillTriggerRequest(idempotency_key="shared-cross-mode-key"),
            "operator",
        )

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.source == "operations")
        )
        drill_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(
                EarthquakeEvent.source == "operations",
                EarthquakeEvent.event_type == "drill",
            )
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))

    assert first.event_id is not None
    assert event_count == 1
    assert drill_count == 0
    assert run_count == 1


async def test_race_losing_trigger_event_is_removed_before_run_insert() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    service = _service()
    service._priority_guard = SequencedPriorityGuard()

    with pytest.raises(RealEventPriorityActive):
        await service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key="race-key"),
            "operator",
        )

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.source == "operations")
        )
        raw_count = await session.scalar(
            select(func.count()).select_from(RawMessage).where(RawMessage.source == "operations")
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))

    assert event_count == 0
    assert raw_count == 0
    assert run_count == 0


async def test_manual_events_still_merge_by_time_and_space() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            existing = EarthquakeEvent(
                source="manual-source",
                canonical_source_id="manual-source:existing",
                event_type="manual",
                origin_time=OBSERVED_AT,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="manual merge fixture",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="not_applicable",
            )
            session.add(existing)
            await session.flush()
            incoming = NormalizedEvent(
                kind=EventKind.MANUAL,
                source="manual-source",
                source_event_id="manual-source:incoming",
                origin_time=OBSERVED_AT,
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="manual merge fixture",
            )
            found = await EventRepository(SessionFactory)._find_canonical_event(
                session,
                incoming,
                "manual-source:manual-source:incoming",
            )

    assert found is not None
    assert found.id == existing.id


async def test_concurrent_cross_mode_same_key_leaves_no_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)

    manual_service = _service()
    drill_service = _service()
    barrier = asyncio.Barrier(2)
    manual_preflight = manual_service._get_run_by_idempotency_key
    drill_preflight = drill_service._get_run_by_idempotency_key

    async def manual_wait(key):
        result = await manual_preflight(key)
        await barrier.wait()
        return result

    async def drill_wait(key):
        result = await drill_preflight(key)
        await barrier.wait()
        return result

    monkeypatch.setattr(
        manual_service,
        "_get_run_by_idempotency_key",
        manual_wait,
    )
    monkeypatch.setattr(
        drill_service,
        "_get_run_by_idempotency_key",
        drill_wait,
    )

    results = await asyncio.gather(
        manual_service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key="concurrent-shared-key"),
            "operator",
        ),
        drill_service.trigger_drill(
            DrillTriggerRequest(idempotency_key="concurrent-shared-key"),
            "operator",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, Exception) for result in results) == 1
    assert any(
        isinstance(result, ValueError) and "idempotency_key conflicts" in str(result)
        for result in results
    )

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.source == "operations")
        )
        raw_count = await session.scalar(
            select(func.count()).select_from(RawMessage).where(RawMessage.source == "operations")
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))

    assert event_count == 1
    assert raw_count == 1
    assert run_count == 1


async def test_manual_key_cannot_impersonate_scheduled_trigger() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)
            schedule = OperationsTestSchedule(
                local_date=OBSERVED_AT.date(),
                window_start=OBSERVED_AT - timedelta(hours=2),
                window_end=OBSERVED_AT,
                scheduled_at=OBSERVED_AT - timedelta(minutes=5),
                state="due",
                random_seed=123,
            )
            session.add(schedule)
            await session.flush()
            schedule_id = schedule.id

    service = _service()
    with pytest.raises(ValueError, match="reserved namespace"):
        await service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key=f"scheduled:{schedule_id}"),
            "operator",
        )

    missing_schedule_id = uuid.uuid4()
    with pytest.raises(ValueError, match="reserved namespace"):
        await service.trigger_manual_test(
            ManualTestTriggerRequest(idempotency_key=f"scheduled:{missing_schedule_id}"),
            "operator",
        )

    scheduled_result = await service.trigger_scheduled_test(
        schedule_id,
        "operations-scheduler",
    )

    async with SessionFactory() as session:
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))
        scheduled_run_count = await session.scalar(
            select(func.count())
            .select_from(OperationsRun)
            .where(OperationsRun.trigger_mode == "scheduled_test")
        )

    assert scheduled_result.event_id is not None
    assert scheduled_result.run_id is not None
    assert run_count == 1
    assert scheduled_run_count == 1


async def test_legacy_scheduler_idempotency_key_reuses_existing_run() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)
            schedule, event, existing_run = await _seed_legacy_scheduled_run(session)

    result = await _service().trigger_scheduled_test(
        schedule.id,
        "operations-scheduler",
    )

    assert result.event_id == event.id
    assert result.run_id == existing_run.id
    assert result.revision_id == existing_run.detail["revision_id"]
    assert result.idempotency_key == existing_run.idempotency_key

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.event_type == "test")
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))
        stored_event = await session.get(EarthquakeEvent, event.id)
        stored_run = await session.get(OperationsRun, existing_run.id)

    assert event_count == 1
    assert run_count == 1
    assert stored_event is not None
    assert stored_run is not None
    assert stored_run.idempotency_key == f"scheduled:{schedule.id}"


async def test_legacy_scheduler_conflict_recovers_existing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await _ensure_boundary(session)
            await _ensure_collaboration_settings(session)
            await OperationsRepository().get_settings(session)
            schedule, event, existing_run = await _seed_legacy_scheduled_run(session)

    service = _service()

    async def miss_preflight(_schedule_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(service, "_get_scheduled_run", miss_preflight)
    result = await service.trigger_scheduled_test(
        schedule.id,
        "operations-scheduler",
    )

    assert result.event_id == event.id
    assert result.run_id == existing_run.id
    assert result.revision_id == existing_run.detail["revision_id"]

    async with SessionFactory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(EarthquakeEvent)
            .where(EarthquakeEvent.event_type == "test")
        )
        run_count = await session.scalar(select(func.count()).select_from(OperationsRun))
        stored_event = await session.get(EarthquakeEvent, event.id)
        stored_run = await session.get(OperationsRun, existing_run.id)

    assert event_count == 1
    assert run_count == 1
    assert stored_event is not None
    assert stored_run is not None
    assert stored_run.event_id == event.id
