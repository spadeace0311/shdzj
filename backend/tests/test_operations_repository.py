from datetime import UTC, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

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
            "test_magnitude_min": Decimal("5.2"),
            "test_magnitude_max": Decimal("5.8"),
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
    assert audit.detail["expected_version"] == 1


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
