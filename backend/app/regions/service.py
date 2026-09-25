from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary
from app.regions.repository import RegionRepository


class RegionContextResolver:
    def __init__(
        self,
        session_factory,
        repository: RegionRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or RegionRepository(session_factory)

    async def resolve(self, longitude: Decimal, latitude: Decimal) -> RegionContext:
        self._validate_coordinates(longitude, latitude)
        computed_at = datetime.now(UTC)

        try:
            async with self._session_factory() as session:
                active = await self._repository.get_active(session)
                if active is None:
                    return self._pending(computed_at)

                query = self._build_query(active, longitude, latitude)
                row = (await session.execute(query)).mappings().one_or_none()
                if row is None:
                    return self._pending(computed_at)

                return self._context_from_row(row, computed_at)
        except (InvalidOperation, SQLAlchemyError, TypeError, ValueError):
            return self._pending(computed_at)

    @staticmethod
    def _build_query(active: Any, longitude: Decimal, latitude: Decimal):
        point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
        boundary_geography = RegionBoundary.geom.cast(Geography)
        point_geography = point.cast(Geography)
        active_id = active.get("id") if isinstance(active, Mapping) else active.id
        active_version = active.get("version") if isinstance(active, Mapping) else active.version

        query = select(
            RegionBoundary.version,
            func.ST_Covers(RegionBoundary.geom, point).label("inside_land"),
            func.ST_DWithin(
                boundary_geography,
                point_geography,
                RegionBoundary.local_buffer_km * 1000,
            ).label("in_local_buffer"),
            (func.ST_Distance(boundary_geography, point_geography) / 1000.0).label("distance_km"),
        ).where(RegionBoundary.is_active.is_(True))

        if active_id is not None:
            return query.where(RegionBoundary.id == active_id)
        return query.where(RegionBoundary.version == active_version)

    @staticmethod
    def _context_from_row(row: Mapping[str, Any], computed_at: datetime) -> RegionContext:
        inside_land = bool(row["inside_land"])
        in_local_buffer = bool(row["in_local_buffer"])
        distance = Decimal(str(row["distance_km"]))
        if not distance.is_finite():
            return RegionContextResolver._pending(computed_at)

        return RegionContext(
            inside_shanghai=inside_land or in_local_buffer,
            distance_to_boundary_km=Decimal("0") if inside_land else max(distance, Decimal("0")),
            boundary_version=str(row["version"]),
            computed_at=computed_at,
        )

    @staticmethod
    def _pending(computed_at: datetime) -> RegionContext:
        return RegionContext(
            inside_shanghai=None,
            distance_to_boundary_km=None,
            boundary_version=None,
            computed_at=computed_at,
        )

    @staticmethod
    def _validate_coordinates(longitude: Decimal, latitude: Decimal) -> None:
        for value, field, minimum, maximum in (
            (longitude, "longitude", Decimal("-180"), Decimal("180")),
            (latitude, "latitude", Decimal("-90"), Decimal("90")),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field} must be a Decimal")
            if not value.is_finite() or value < minimum or value > maximum:
                raise ValueError(f"{field} must be between {minimum} and {maximum}")
