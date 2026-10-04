from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from geoalchemy2 import Geography
from sqlalchemy import and_, func, not_, select
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
        self._repository = repository

    async def resolve(self, longitude: Decimal, latitude: Decimal) -> RegionContext:
        self._validate_coordinates(longitude, latitude)
        computed_at = datetime.now(UTC)

        try:
            async with self._session_factory() as session:
                query = self._build_query(longitude, latitude)
                row = (await session.execute(query)).mappings().one_or_none()
                if row is None:
                    return self._pending(computed_at)

                return self._context_from_row(row, computed_at)
        except (InvalidOperation, SQLAlchemyError, TypeError, ValueError):
            return self._pending(computed_at)

    @staticmethod
    def _build_query(longitude: Decimal, latitude: Decimal):
        point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
        boundary_geography = RegionBoundary.geom.cast(Geography)
        point_geography = point.cast(Geography)
        inside_land = func.ST_Covers(RegionBoundary.geom, point)
        inside_offshore_sea = and_(
            not_(inside_land),
            func.ST_DWithin(
                boundary_geography,
                point_geography,
                RegionBoundary.local_buffer_km * 1000,
            ),
            func.ST_Covers(RegionBoundary.maritime_geom, point),
        )

        return (
            select(
                RegionBoundary.version,
                inside_land.label("inside_land"),
                inside_offshore_sea.label("inside_offshore_sea"),
                (func.ST_Distance(boundary_geography, point_geography) / 1000.0).label(
                    "distance_km"
                ),
            )
            .where(RegionBoundary.is_active.is_(True))
            .limit(1)
        )

    @staticmethod
    def _context_from_row(row, computed_at: datetime) -> RegionContext:
        inside_land = bool(row["inside_land"])
        inside_offshore_sea = bool(row["inside_offshore_sea"])
        distance = Decimal(str(row["distance_km"]))
        if not distance.is_finite():
            return RegionContextResolver._pending(computed_at)

        return RegionContext(
            inside_shanghai=inside_land or inside_offshore_sea,
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
