import asyncio
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.events.models import EarthquakeEvent
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.repository import OperationsRepository, StaleOperationsSettings


@pytest.fixture(autouse=True)
async def _dispose_database_engine():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def _reset_repository_tables(session) -> None:
    await session.execute(delete(OperationsRun))
    await session.execute(delete(OperationsTestSchedule))
    await session.execute(delete(OperationsAuditLog))
    await session.execute(delete(OperationsSettings))
    await session.flush()


async def _add_schedule(session, local_date: date) -> OperationsTestSchedule:
    schedule = OperationsTestSchedule(
        local_date=local_date,
        window_start=datetime(2026, 10, 10, 2, tzinfo=UTC),
        window_end=datetime(2026, 10, 10, 4, tzinfo=UTC),
        scheduled_at=datetime(2026, 10, 9, 18, tzinfo=UTC),
        state="scheduled",
        random_seed=20261010,
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def _add_event_and_run(
    session,
) -> tuple[EarthquakeEvent, OperationsRun]:
    event = EarthquakeEvent(
        id=uuid.uuid4(),
        source="operations-test-fixture",
        canonical_source_id=f"operations-fixture:{uuid.uuid4()}",
        event_type="test",
        origin_time=datetime(2026, 10, 10, 2, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.5"),
        place="上海市（系统测试）",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
        lifecycle_state="not_applicable",
    )
    session.add(event)
    await session.flush()
    run = OperationsRun(
        event_id=event.id,
        trigger_mode="scheduled_test",
        operation_state="queued",
        priority=10,
        idempotency_key=f"operations-fixture:{uuid.uuid4()}",
        triggered_by="operations-test-fixture",
        detail={},
    )
    session.add(run)
    await session.flush()
    return event, run


async def test_get_settings_returns_default(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()

    settings = await repository.get_settings(session)

    assert settings.settings_key == "default"
    assert settings.daily_test_enabled is True
    assert settings.timezone == "Asia/Shanghai"
    assert settings.daily_window_start == time(2, 0)
    assert settings.daily_window_end == time(4, 0)
    assert settings.test_magnitude_min == Decimal("5.0")
    assert settings.test_magnitude_max == Decimal("5.9")
    assert settings.test_depth_min_km == Decimal("5.00")
    assert settings.test_depth_max_km == Decimal("20.00")
    assert settings.drill_conflict_policy == "pause"
    assert settings.row_version == 1


async def test_get_settings_is_idempotent(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()

    first = await repository.get_settings(session)
    second = await repository.get_settings(session)
    ids = (await session.scalars(select(OperationsSettings.id))).all()

    assert first.id == second.id
    assert len(ids) == 1


async def test_get_settings_concurrent_initialization_is_idempotent() -> None:
    from app.db import SessionFactory

    async with SessionFactory() as cleanup:
        async with cleanup.begin():
            await cleanup.execute(delete(OperationsSettings))

    async def initialize() -> uuid.UUID:
        async with SessionFactory() as session:
            async with session.begin():
                settings = await OperationsRepository().get_settings(session)
                return settings.id

    first_id, second_id = await asyncio.gather(initialize(), initialize())

    assert first_id == second_id


async def test_update_settings_rejects_stale_version(session) -> None:
    await _reset_repository_tables(session)
    session.add(
        OperationsSettings(
            settings_key="default",
            row_version=3,
        )
    )
    await session.flush()

    with pytest.raises(StaleOperationsSettings) as exc_info:
        await OperationsRepository().update_settings(
            session,
            "operations-test",
            2,
            {"daily_test_enabled": False},
        )

    assert exc_info.value.expected_version == 2
    assert exc_info.value.actual_version == 3


async def test_update_settings_writes_audit_and_versions(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()
    settings = await repository.get_settings(session)
    observed_at = datetime(2026, 10, 10, 2, tzinfo=UTC)

    updated = await repository.update_settings(
        session,
        "operations-test",
        settings.row_version,
        {
            "daily_window_start": time(3, 0),
            "daily_window_end": time(5, 0),
            "test_magnitude_min": Decimal("5.24"),
            "test_magnitude_max": Decimal("5.8"),
            "test_depth_min_km": Decimal("10.234"),
            "drill_conflict_policy": "terminate",
        },
        observed_at=observed_at,
    )

    assert updated.row_version == 2
    assert updated.updated_by == "operations-test"
    assert updated.updated_at == observed_at.astimezone(UTC)
    assert updated.daily_window_start == time(3, 0)
    assert updated.daily_window_end == time(5, 0)
    assert updated.test_magnitude_min == Decimal("5.2")
    assert updated.test_magnitude_max == Decimal("5.8")
    assert updated.test_depth_min_km == Decimal("10.23")
    assert updated.drill_conflict_policy == "terminate"

    audit = await session.scalar(
        select(OperationsAuditLog)
        .where(OperationsAuditLog.action == "settings.updated")
        .order_by(OperationsAuditLog.created_at.desc(), OperationsAuditLog.id.desc())
        .limit(1)
    )
    assert audit is not None
    assert audit.actor == "operations-test"
    assert audit.resource_type == "operations_settings"
    assert audit.resource_id == str(settings.id)
    assert audit.before_state["row_version"] == 1
    assert audit.after_state["row_version"] == 2
    assert audit.after_state["test_magnitude_min"] == "5.2"
    assert audit.after_state["test_depth_min_km"] == "10.23"
    assert audit.detail["expected_version"] == 1


async def test_update_settings_initializes_missing_row(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()

    updated = await repository.update_settings(
        session,
        "operations-test",
        1,
        {"daily_test_enabled": False},
    )
    stored = await repository.get_settings(session)

    assert updated.row_version == 2
    assert updated.daily_test_enabled is False
    assert stored.id == updated.id


async def test_get_schedule_returns_same_row_and_none(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()
    local_date = date(2026, 10, 10)
    schedule = await _add_schedule(session, local_date)

    first = await repository.get_schedule(session, local_date)
    second = await repository.get_schedule(session, local_date)
    missing = await repository.get_schedule(session, date(2026, 10, 11))

    assert first is not None
    assert first.id == schedule.id
    assert second.id == first.id
    assert missing is None


async def test_get_run_for_event_returns_unique_run_and_none(session) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()
    event, run = await _add_event_and_run(session)

    found = await repository.get_run_for_event(session, event.id)
    missing = await repository.get_run_for_event(session, uuid.uuid4())

    assert found is not None
    assert found.id == run.id
    assert missing is None
    await session.execute(delete(EarthquakeEvent).where(EarthquakeEvent.id == event.id))


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            {"daily_window_start": time(4, 0), "daily_window_end": time(4, 0)},
            "daily_window_start",
        ),
        (
            {"test_magnitude_min": Decimal("6.0"), "test_magnitude_max": Decimal("5.0")},
            "test_magnitude_min",
        ),
        (
            {"drill_conflict_policy": "unknown"},
            "drill_conflict_policy",
        ),
        (
            {"test_magnitude_min": 5.0},
            "test_magnitude_min",
        ),
        (
            {"test_magnitude_min": Decimal("-2.1")},
            "test_magnitude_min",
        ),
        (
            {"test_depth_min_km": Decimal("-0.01")},
            "test_depth_min_km",
        ),
        (
            {"test_depth_min_km": Decimal("1000.01")},
            "test_depth_min_km",
        ),
    ],
)
async def test_update_settings_rejects_invalid_patch(
    session,
    patch,
    message,
) -> None:
    await _reset_repository_tables(session)
    repository = OperationsRepository()
    settings = await repository.get_settings(session)

    with pytest.raises(ValueError, match=message):
        await repository.update_settings(
            session,
            "operations-test",
            settings.row_version,
            patch,
        )
