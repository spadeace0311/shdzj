import json
from decimal import Decimal

import pytest
from sqlalchemy import delete, event, func, inspect, select

from app.db import SessionFactory, engine
from app.regions.importer import import_geojson
from app.regions.models import RegionBoundary
from app.regions.service import RegionContextResolver


FIXTURE_PATH = "/app/tests/fixtures/shanghai_boundary.geojson"
SOURCE_URI = "https://example.gov.invalid/regions/shanghai-synthetic.geojson"


@pytest.fixture(autouse=True)
async def dispose_database_engine():
    yield
    await engine.dispose()


class FailingRepository:
    async def get_active(self, session):
        raise AssertionError("resolver must not call the legacy repository lookup")


async def test_resolver_distinguishes_administrative_area_sea_buffer_and_land() -> None:
    resolver = RegionContextResolver(SessionFactory)
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                FIXTURE_PATH,
                version="test-2026.1",
                name="test-shanghai",
                source_uri=SOURCE_URI,
                activate=True,
            )

    inside = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))
    west_neighbor_land = await resolver.resolve(Decimal("120.7"), Decimal("31.2"))
    east_maritime_buffer = await resolver.resolve(Decimal("122.3"), Decimal("31.2"))
    outside = await resolver.resolve(Decimal("122.9"), Decimal("31.2"))

    assert inside.inside_shanghai is True
    assert inside.distance_to_boundary_km == Decimal("0")
    assert inside.boundary_version == "test-2026.1"
    assert west_neighbor_land.inside_shanghai is False
    assert west_neighbor_land.distance_to_boundary_km <= Decimal("50")
    assert east_maritime_buffer.inside_shanghai is True
    assert east_maritime_buffer.distance_to_boundary_km <= Decimal("50")
    assert outside.inside_shanghai is False
    assert outside.distance_to_boundary_km > Decimal("50")


async def test_resolver_returns_pending_without_boundary() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))

    resolver = RegionContextResolver(SessionFactory)
    result = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))

    assert result.inside_shanghai is None
    assert result.distance_to_boundary_km is None
    assert result.boundary_version is None


async def test_missing_maritime_geometry_does_not_include_offshore_buffer(tmp_path) -> None:
    source = tmp_path / "administrative-only.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"role": "administrative_boundary"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [120.8, 30.6],
                                    [122.2, 30.6],
                                    [122.2, 31.9],
                                    [120.8, 31.9],
                                    [120.8, 30.6],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                source,
                version="test-no-sea",
                name="test-shanghai",
                source_uri=SOURCE_URI,
                activate=True,
            )

    resolver = RegionContextResolver(SessionFactory)
    inside = await resolver.resolve(Decimal("121.5"), Decimal("31.2"))
    offshore = await resolver.resolve(Decimal("122.3"), Decimal("31.2"))

    assert inside.inside_shanghai is True
    assert offshore.inside_shanghai is False
    assert offshore.distance_to_boundary_km <= Decimal("50")


async def test_resolve_uses_one_active_boundary_select_without_legacy_lookup() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                FIXTURE_PATH,
                version="test-2026.1",
                name="test-shanghai",
                source_uri=SOURCE_URI,
                activate=True,
            )

    select_statements: list[str] = []

    def capture_select(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    resolver = RegionContextResolver(
        SessionFactory,
        repository=FailingRepository(),
    )
    event.listen(engine.sync_engine, "before_cursor_execute", capture_select)
    try:
        result = await resolver.resolve(Decimal("122.3"), Decimal("31.2"))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_select)

    assert result.inside_shanghai is True
    assert result.boundary_version == "test-2026.1"
    assert len(select_statements) == 1
    assert "FROM region_boundaries" in select_statements[0]
    assert "region_boundaries.is_active" in select_statements[0]


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
        (
            {
                "type": "Polygon",
                "coordinates": [[[179, 30], [-179, 30], [-179, 31], [179, 30]]],
            },
            "antimeridian",
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
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"role": "administrative_boundary"},
                        "geometry": geometry,
                    }
                ],
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
                    source_uri=SOURCE_URI,
                    activate=False,
                )


async def test_importer_requires_explicit_remote_source_uri(tmp_path) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match="source_uri"):
                await import_geojson(
                    session,
                    FIXTURE_PATH,
                    version="missing-source",
                    name="test-shanghai",
                    activate=False,
                )
            with pytest.raises(ValueError, match="source_uri"):
                await import_geojson(
                    session,
                    tmp_path / "local.geojson",
                    version="local-source",
                    name="test-shanghai",
                    source_uri=str(tmp_path / "local.geojson"),
                    activate=False,
                )


def _inspect_region_boundary_columns(connection):
    return {
        column["name"]: column for column in inspect(connection).get_columns("region_boundaries")
    }


async def test_region_boundary_audit_columns_are_not_null() -> None:
    async with engine.connect() as connection:
        columns = await connection.run_sync(_inspect_region_boundary_columns)

    assert columns["source_uri"]["nullable"] is False
    assert columns["checksum"]["nullable"] is False
    assert columns["maritime_geom"]["nullable"] is False


async def test_importing_active_version_deactivates_the_previous_version() -> None:
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(delete(RegionBoundary))
            await import_geojson(
                session,
                FIXTURE_PATH,
                version="test-2026.1",
                name="test-shanghai",
                source_uri=SOURCE_URI,
                activate=True,
            )
            await import_geojson(
                session,
                FIXTURE_PATH,
                version="test-2026.2",
                name="test-shanghai",
                source_uri=SOURCE_URI,
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
