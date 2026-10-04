from decimal import Decimal
from uuid import uuid4

import pytest
from geoalchemy2.elements import WKTElement
from pyproj import Transformer
from shapely import wkt as shapely_wkt
from shapely.ops import transform
from sqlalchemy import delete, update

from app.data_assets.models import DataAssetVersion
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import DataAssetService
from app.regions.models import RegionBoundary
from tests.data_asset_helpers import (
    FIXTURE_ACTOR,
    _populate,
    _queue_candidate,
    _town_records,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_fixture_versions(session_factory):
    from tests.data_asset_helpers import _cleanup_fixture_data

    yield
    await _cleanup_fixture_data(session_factory)


async def test_full_coverage_asset_records_projected_area_and_sufficient_status(
    session_factory,
) -> None:
    boundary_version = await _activate_boundary(
        session_factory,
        "MULTIPOLYGON (((121.4 31.1, 121.6 31.1, 121.6 31.4, "
        "121.4 31.4, 121.4 31.1)))",
    )
    try:
        report, version_summary = await _validate_admin_town(session_factory)
    finally:
        await _delete_boundary(session_factory, boundary_version)

    assert report.publishable is True
    assert report.statistics["area_crs"] == "EPSG:32651"
    assert report.statistics["coverage_ratio"] == pytest.approx(1.0)
    assert report.statistics["coverage_status"] == "sufficient"
    assert report.statistics["coverage_policy_version"] == (
        "shanghai-data-asset-coverage-v1"
    )
    geometry = shapely_wkt.loads(_town_records().records[0].geometry_wkt)
    expected_area = transform(
        Transformer.from_crs(
            "EPSG:4326",
            "EPSG:32651",
            always_xy=True,
        ).transform,
        geometry,
    ).area
    assert report.statistics["projected_area"] == pytest.approx(
        expected_area,
        rel=1e-9,
    )
    assert version_summary["validation_statistics"] == report.statistics


async def test_insufficient_full_coverage_is_rejected(
    session_factory,
) -> None:
    boundary_version = await _activate_boundary(
        session_factory,
        "MULTIPOLYGON (((121.4 31.1, 121.44 31.1, 121.44 31.4, "
        "121.4 31.4, 121.4 31.1)))",
    )
    try:
        report, _ = await _validate_admin_town(session_factory)
    finally:
        await _delete_boundary(session_factory, boundary_version)

    assert report.publishable is False
    assert report.statistics["coverage_status"] == "insufficient"
    assert report.statistics["coverage_ratio"] == pytest.approx(
        0.2,
        rel=1e-3,
    )
    assert any(
        issue.code == "region_coverage_insufficient"
        for issue in report.errors
    )


async def _activate_boundary(
    session_factory,
    geometry_wkt: str,
) -> str:
    version = f"data-asset-coverage-{uuid4()}"
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(RegionBoundary).values(is_active=False)
            )
            session.add(
                RegionBoundary(
                    version=version,
                    name="data asset coverage test",
                    local_buffer_km=Decimal("0"),
                    geom=WKTElement(geometry_wkt, srid=4326),
                    maritime_geom=WKTElement(geometry_wkt, srid=4326),
                    source_uri="https://example.gov.invalid/coverage",
                    checksum="b" * 64,
                    is_active=True,
                )
            )
    return version


async def _delete_boundary(session_factory, version: str) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(RegionBoundary).where(RegionBoundary.version == version)
            )


async def _validate_admin_town(session_factory):
    definition = get_asset_definition("shanghai.admin.town")
    normalized = _town_records()
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key=definition.asset_key,
                version=f"coverage-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                normalized,
                importer="geojson",
            )
            report = await DataAssetService().validate_version(
                session,
                job.asset_version_id,
                actor=FIXTURE_ACTOR,
            )
            version = await session.get(
                DataAssetVersion,
                job.asset_version_id,
            )
            return report, dict(version.schema_summary)
