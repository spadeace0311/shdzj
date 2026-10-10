from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)


class StaleOperationsSettings(Exception):
    def __init__(self, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            "expected operations settings version "
            f"{expected_version}, found {actual_version}"
        )


class OperationsRepository:
    SETTINGS_KEY = "default"
    _PATCH_FIELDS = (
        "daily_test_enabled",
        "timezone",
        "daily_window_start",
        "daily_window_end",
        "test_magnitude_min",
        "test_magnitude_max",
        "test_depth_min_km",
        "test_depth_max_km",
        "drill_conflict_policy",
    )

    async def get_settings(self, session: AsyncSession) -> OperationsSettings:
        settings = await session.scalar(
            select(OperationsSettings)
            .where(OperationsSettings.settings_key == self.SETTINGS_KEY)
            .order_by(OperationsSettings.created_at, OperationsSettings.id)
            .limit(1)
        )
        if settings is None:
            settings = OperationsSettings(settings_key=self.SETTINGS_KEY)
            session.add(settings)
            await session.flush()
        return settings

    async def update_settings(
        self,
        session: AsyncSession,
        actor: str,
        expected_version: int,
        patch: dict,
        *,
        observed_at: datetime | None = None,
    ) -> OperationsSettings:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-empty string")
        if not isinstance(patch, dict):
            raise TypeError("patch must be a dictionary")
        unknown_fields = set(patch) - set(self._PATCH_FIELDS)
        if unknown_fields:
            field = sorted(unknown_fields)[0]
            raise ValueError(f"unsupported_settings_field: {field}")
        observed_at = _require_aware_datetime(observed_at or datetime.now(UTC))

        settings = await session.scalar(
            select(OperationsSettings)
            .where(OperationsSettings.settings_key == self.SETTINGS_KEY)
            .order_by(OperationsSettings.created_at, OperationsSettings.id)
            .limit(1)
            .with_for_update()
        )
        if settings is None:
            settings = OperationsSettings(settings_key=self.SETTINGS_KEY)
            session.add(settings)
            await session.flush()

        if int(settings.row_version) != int(expected_version):
            raise StaleOperationsSettings(
                expected_version=int(expected_version),
                actual_version=int(settings.row_version),
            )

        before = _settings_state(settings)
        _validate_patch(settings, patch)
        for field, value in patch.items():
            setattr(settings, field, value)
        settings.row_version += 1
        settings.updated_by = actor
        settings.updated_at = observed_at
        after = _settings_state(settings)

        session.add(
            OperationsAuditLog(
                action="settings.updated",
                actor=actor,
                resource_type="operations_settings",
                resource_id=str(settings.id),
                before_state=before,
                after_state=after,
                detail={"expected_version": int(expected_version)},
                created_at=observed_at,
            )
        )
        await session.flush()
        return settings

    async def get_schedule(
        self,
        session: AsyncSession,
        local_date: date,
    ) -> OperationsTestSchedule | None:
        return await session.scalar(
            select(OperationsTestSchedule)
            .where(OperationsTestSchedule.local_date == local_date)
            .order_by(
                OperationsTestSchedule.created_at.desc(),
                OperationsTestSchedule.id.desc(),
            )
            .limit(1)
        )

    async def get_run_for_event(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> OperationsRun | None:
        return await session.scalar(
            select(OperationsRun)
            .where(OperationsRun.event_id == event_id)
            .order_by(
                OperationsRun.created_at.desc(),
                OperationsRun.id.desc(),
            )
            .limit(1)
        )


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)


def _validate_patch(
    settings: OperationsSettings,
    patch: dict,
) -> None:
    start = patch.get("daily_window_start", settings.daily_window_start)
    end = patch.get("daily_window_end", settings.daily_window_end)
    if not isinstance(start, time) or not isinstance(end, time):
        raise TypeError("daily window values must be time objects")
    if start >= end:
        raise ValueError("daily_window_start must be before daily_window_end")

    magnitude_min = patch.get(
        "test_magnitude_min",
        settings.test_magnitude_min,
    )
    magnitude_max = patch.get(
        "test_magnitude_max",
        settings.test_magnitude_max,
    )
    if magnitude_min > magnitude_max:
        raise ValueError(
            "test_magnitude_min must be less than or equal to test_magnitude_max"
        )

    depth_min = patch.get("test_depth_min_km", settings.test_depth_min_km)
    depth_max = patch.get("test_depth_max_km", settings.test_depth_max_km)
    if depth_min > depth_max:
        raise ValueError(
            "test_depth_min_km must be less than or equal to test_depth_max_km"
        )

    policy = patch.get(
        "drill_conflict_policy",
        settings.drill_conflict_policy,
    )
    if policy not in {"pause", "terminate"}:
        raise ValueError("drill_conflict_policy must be pause or terminate")


def _settings_state(settings: OperationsSettings) -> dict:
    return {
        "settings_key": settings.settings_key,
        "daily_test_enabled": settings.daily_test_enabled,
        "timezone": settings.timezone,
        "daily_window_start": settings.daily_window_start.isoformat(),
        "daily_window_end": settings.daily_window_end.isoformat(),
        "test_magnitude_min": str(settings.test_magnitude_min),
        "test_magnitude_max": str(settings.test_magnitude_max),
        "test_depth_min_km": str(settings.test_depth_min_km),
        "test_depth_max_km": str(settings.test_depth_max_km),
        "drill_conflict_policy": settings.drill_conflict_policy,
        "row_version": int(settings.row_version),
    }
