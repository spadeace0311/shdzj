from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.scenario import OperationsConfigurationError


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
    MAGNITUDE_QUANTUM = Decimal("0.1")
    MAGNITUDE_MIN = Decimal("-2.0")
    MAGNITUDE_MAX = Decimal("12.0")
    DEPTH_QUANTUM = Decimal("0.01")
    DEPTH_MIN = Decimal("0")
    DEPTH_MAX = Decimal("1000")
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
        return await self._get_or_create_settings(session)

    async def _get_or_create_settings(
        self,
        session: AsyncSession,
        *,
        for_update: bool = False,
        validate_timezone: bool = True,
    ) -> OperationsSettings:
        settings = await session.scalar(_settings_select(for_update=for_update))
        if settings is not None:
            if validate_timezone:
                _validate_timezone(settings.timezone)
            return settings

        await session.execute(
            pg_insert(OperationsSettings)
            .values(_default_settings_insert_values())
            .on_conflict_do_nothing(
                index_elements=[OperationsSettings.settings_key]
            )
        )
        settings = await session.scalar(_settings_select(for_update=for_update))
        if settings is None:
            raise RuntimeError("operations settings could not be initialized")
        if validate_timezone:
            _validate_timezone(settings.timezone)
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

        settings = await self._get_or_create_settings(
            session,
            for_update=True,
            validate_timezone=False,
        )

        if int(settings.row_version) != int(expected_version):
            raise StaleOperationsSettings(
                expected_version=int(expected_version),
                actual_version=int(settings.row_version),
            )

        before = _settings_state(settings)
        normalized_patch = self._normalize_patch(settings, patch)
        _validate_timezone(normalized_patch.get("timezone", settings.timezone))
        for field, value in normalized_patch.items():
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

    def _normalize_patch(
        self,
        settings: OperationsSettings,
        patch: dict,
    ) -> dict:
        normalized: dict = {}

        if "daily_test_enabled" in patch:
            if not isinstance(patch["daily_test_enabled"], bool):
                raise ValueError("daily_test_enabled must be a bool")
            normalized["daily_test_enabled"] = patch["daily_test_enabled"]

        if "timezone" in patch:
            timezone = patch["timezone"]
            if not isinstance(timezone, str) or not timezone.strip():
                raise ValueError("timezone must be a non-empty string")
            normalized["timezone"] = _validated_timezone(timezone)

        start = patch.get("daily_window_start", settings.daily_window_start)
        end = patch.get("daily_window_end", settings.daily_window_end)
        if not isinstance(start, time) or not isinstance(end, time):
            raise ValueError("daily window values must be time objects")
        if start >= end:
            raise ValueError("daily_window_start must be before daily_window_end")
        if "daily_window_start" in patch:
            normalized["daily_window_start"] = start
        if "daily_window_end" in patch:
            normalized["daily_window_end"] = end

        self._normalize_decimal_field(
            patch,
            normalized,
            "test_magnitude_min",
            self.MAGNITUDE_QUANTUM,
            self.MAGNITUDE_MIN,
            self.MAGNITUDE_MAX,
        )
        self._normalize_decimal_field(
            patch,
            normalized,
            "test_magnitude_max",
            self.MAGNITUDE_QUANTUM,
            self.MAGNITUDE_MIN,
            self.MAGNITUDE_MAX,
        )
        if normalized.get(
            "test_magnitude_min",
            settings.test_magnitude_min,
        ) > normalized.get(
            "test_magnitude_max",
            settings.test_magnitude_max,
        ):
            raise ValueError(
                "test_magnitude_min must be less than or equal to test_magnitude_max"
            )

        self._normalize_decimal_field(
            patch,
            normalized,
            "test_depth_min_km",
            self.DEPTH_QUANTUM,
            self.DEPTH_MIN,
            self.DEPTH_MAX,
        )
        self._normalize_decimal_field(
            patch,
            normalized,
            "test_depth_max_km",
            self.DEPTH_QUANTUM,
            self.DEPTH_MIN,
            self.DEPTH_MAX,
        )
        if normalized.get(
            "test_depth_min_km",
            settings.test_depth_min_km,
        ) > normalized.get(
            "test_depth_max_km",
            settings.test_depth_max_km,
        ):
            raise ValueError(
                "test_depth_min_km must be less than or equal to test_depth_max_km"
            )

        if "drill_conflict_policy" in patch:
            policy = patch["drill_conflict_policy"]
            if policy not in {"pause", "terminate"}:
                raise ValueError("drill_conflict_policy must be pause or terminate")
            normalized["drill_conflict_policy"] = policy

        return normalized

    def _normalize_decimal_field(
        self,
        patch: dict,
        normalized: dict,
        field: str,
        quantum: Decimal,
        minimum: Decimal,
        maximum: Decimal,
    ) -> None:
        if field not in patch:
            return
        value = patch[field]
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{field} must be a finite Decimal")
        if value < minimum or value > maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        normalized[field] = value.quantize(quantum, rounding=ROUND_HALF_UP)

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


def _settings_select(*, for_update: bool = False):
    statement = (
        select(OperationsSettings)
        .where(OperationsSettings.settings_key == OperationsRepository.SETTINGS_KEY)
        .order_by(OperationsSettings.created_at, OperationsSettings.id)
        .limit(1)
    )
    return statement.with_for_update() if for_update else statement


def _default_settings_insert_values() -> dict:
    return {
        "id": uuid.uuid4(),
        "settings_key": OperationsRepository.SETTINGS_KEY,
        "daily_test_enabled": True,
        "timezone": "Asia/Shanghai",
        "daily_window_start": time(2, 0),
        "daily_window_end": time(4, 0),
        "test_magnitude_min": Decimal("5.0"),
        "test_magnitude_max": Decimal("5.9"),
        "test_depth_min_km": Decimal("5.00"),
        "test_depth_max_km": Decimal("20.00"),
        "drill_conflict_policy": "pause",
        "row_version": 1,
    }


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


def _validated_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid_timezone") from exc
    return value.strip()


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OperationsConfigurationError(
            "invalid_operations_timezone"
        ) from exc
