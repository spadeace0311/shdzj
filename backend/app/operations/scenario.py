import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.domain import EventKind, NormalizedEvent
from app.operations.domain import OperationsScenario
from app.operations.models import OperationsSettings
from app.regions.models import RegionBoundary


class OperationsConfigurationError(RuntimeError):
    pass


class ScenarioGenerator:
    PLACE = "上海市（系统测试）"
    SOURCE = "operations"
    MAGNITUDE_STEP = Decimal("0.1")
    DEPTH_STEP = Decimal("0.01")

    async def generate_test(
        self,
        session: AsyncSession,
        settings: OperationsSettings,
        *,
        observed_at: datetime,
        seed: int,
    ) -> OperationsScenario:
        observed_at = _require_aware_datetime(observed_at, "observed_at")
        point = await self._active_boundary_point(session)
        if point is None:
            raise OperationsConfigurationError(
                "active_shanghai_boundary_required"
            )

        rng = random.Random(seed)
        magnitude = _sample_decimal(
            rng,
            settings.test_magnitude_min,
            settings.test_magnitude_max,
            self.MAGNITUDE_STEP,
        )
        depth_km = _sample_decimal(
            rng,
            settings.test_depth_min_km,
            settings.test_depth_max_km,
            self.DEPTH_STEP,
        )
        longitude, latitude = point
        observed_utc = observed_at.astimezone(UTC)
        source_event_id = (
            f"operations-test:{observed_utc.strftime('%Y%m%d')}:{seed}"
        )
        return OperationsScenario(
            origin_time=observed_utc,
            longitude=longitude,
            latitude=latitude,
            depth_km=depth_km,
            magnitude=magnitude,
            place=self.PLACE,
            source_event_id=source_event_id,
            random_seed=seed,
        )

    def scenario_to_event(self, scenario: OperationsScenario) -> NormalizedEvent:
        return NormalizedEvent(
            kind=EventKind.TEST,
            source=self.SOURCE,
            source_event_id=scenario.source_event_id,
            origin_time=scenario.origin_time,
            longitude=scenario.longitude,
            latitude=scenario.latitude,
            depth_km=scenario.depth_km,
            magnitude=scenario.magnitude,
            place=scenario.place,
        )

    async def region_contains(
        self,
        session: AsyncSession,
        scenario: OperationsScenario,
    ) -> bool:
        point = func.ST_SetSRID(
            func.ST_MakePoint(scenario.longitude, scenario.latitude),
            4326,
        )
        value = await session.scalar(
            select(func.ST_Covers(RegionBoundary.geom, point))
            .where(RegionBoundary.is_active.is_(True))
            .order_by(RegionBoundary.id)
            .limit(1)
        )
        if value is None:
            raise OperationsConfigurationError(
                "active_shanghai_boundary_required"
            )
        return bool(value)

    @staticmethod
    async def _active_boundary_point(
        session: AsyncSession,
    ) -> tuple[Decimal, Decimal] | None:
        row = (
            await session.execute(
                select(
                    func.ST_X(
                        func.ST_PointOnSurface(RegionBoundary.geom)
                    ).label("longitude"),
                    func.ST_Y(
                        func.ST_PointOnSurface(RegionBoundary.geom)
                    ).label("latitude"),
                )
                .where(RegionBoundary.is_active.is_(True))
                .order_by(RegionBoundary.id)
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return (
            Decimal(str(row["longitude"])),
            Decimal(str(row["latitude"])),
        )


def _sample_decimal(
    rng: random.Random,
    minimum: Decimal,
    maximum: Decimal,
    step: Decimal,
) -> Decimal:
    span = maximum - minimum
    step_count = int((span / step).to_integral_value(rounding=ROUND_HALF_UP))
    offset = rng.randrange(0, step_count + 1)
    return (minimum + step * offset).quantize(step, rounding=ROUND_HALF_UP)


def _require_aware_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value
