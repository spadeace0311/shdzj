from decimal import Decimal
import json

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionFactory, engine
from app.regions.importer import import_geojson
from app.regions.models import RegionBoundary
from app.regions.service import RegionContextResolver


@pytest.fixture(autouse=True)
async def dispose_database_engine():
    yield
    await engine.dispose()


class EmptyRepository:
    async def get_active(self, session):
        return None


class TestRepository:
    async def get_active(self, session):
        result = await session.execute(
            RegionBoundary.__table__.select().where(RegionBoundary.is_active)
        )
        return result.mappings().first()


async def test_resolver_resolves_inside_and_outside_with_real_postgis() -> None:
    repository = TestRepository()
    resolver = RegionContextResolver(SessionFactory, repository=repository)
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                "/app/tests/fixtures/shanghai_boundary.geojson",
                version="test-2026.1",
                name="test-shanghai",
                activate=True,
            )

    inside = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))
    in_offshore_buffer = await resolver.resolve(Decimal("122.3"), Decimal("31.2"))
    outside = await resolver.resolve(Decimal("122.9"), Decimal("31.2"))

    assert inside.inside_shanghai is True
    assert inside.distance_to_boundary_km == Decimal("0")
    assert inside.boundary_version == "test-2026.1"
    assert in_offshore_buffer.inside_shanghai is True
    assert in_offshore_buffer.distance_to_boundary_km > 0
    assert outside.inside_shanghai is False
    assert outside.distance_to_boundary_km > 0


async def test_resolver_returns_pending_without_boundary() -> None:
    resolver = RegionContextResolver(
        SessionFactory,
        repository=EmptyRepository(),
    )

    result = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))

    assert result.inside_shanghai is None
    assert result.distance_to_boundary_km is None
    assert result.boundary_version is None


@pytest.mark.parametrize(
    ("geometry", "message"),
    [
        ({"type": "Polygon", "coordinates": []}, "empty"),
        (
            {
                "type": "Polygon",
                "coordinates": [[[181, 31], [122, 31], [121, 30], [181, 31]]],
            },
            "longitude",
        ),
        (
            {
                "type": "Polygon",
                "coordinates": [[[121, 91], [122, 31], [121, 30], [121, 91]]],
            },
            "latitude",
        ),
    ],
)
async def test_importer_rejects_invalid_geometry(
    tmp_path,
    geometry: dict[str, object],
    message: str,
) -> None:
    source = tmp_path / "invalid.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": geometry}],
            }
        ),
        encoding="utf-8",
    )

    async with SessionFactory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match=message):
                await import_geojson(
                    session,
                    source,
                    version="invalid",
                    name="invalid",
                    activate=False,
                )


async def test_importing_active_version_deactivates_the_previous_version() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                "/app/tests/fixtures/shanghai_boundary.geojson",
                version="test-2026.1",
                name="test-shanghai",
                activate=True,
            )
            await import_geojson(
                session,
                "/app/tests/fixtures/shanghai_boundary.geojson",
                version="test-2026.2",
                name="test-shanghai",
                activate=True,
            )

            active_versions = await session.scalars(
                select(RegionBoundary.version).where(RegionBoundary.is_active)
            )
            active_count = await session.scalar(
                select(func.count()).select_from(RegionBoundary).where(RegionBoundary.is_active)
            )

    assert active_versions.all() == ["test-2026.2"]
    assert active_count == 1
