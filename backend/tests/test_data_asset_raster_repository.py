import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from app.data_assets.models import DataAssetVersion
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.raster_repository import (
    _verify_saved_raster,
    load_raster_version,
    save_raster_version,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def seeded_compressed_imported_version(session_factory, tmp_path):
    from datetime import UTC, datetime

    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from sqlalchemy import select

    from app.data_assets.models import DataAsset, DataAssetVersion
    from tests.data_asset_helpers import (
        FIXTURE_ACTOR,
        SeededImportedVersion,
        _asset_contract,
        _cleanup_fixture_data,
        _definition,
    )

    source_path = tmp_path / "compressed.tif"
    transform = Affine(0.01, 0, 121.0, 0, -0.01, 31.5)
    with rasterio.open(
        source_path,
        "w",
        driver="GTiff",
        width=16,
        height=16,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        nodata=0,
        transform=transform,
        compress="DEFLATE",
        tiled=True,
        blockxsize=16,
        blockysize=16,
    ) as target:
        target.write(np.arange(256, dtype="uint8").reshape(16, 16), 1)

    definition = _definition("shanghai.gdp.raster")
    source_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == definition.asset_key,
                    DataAsset.region_id == definition.region_id,
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=definition.asset_key,
                    region_id=definition.region_id,
                    name=definition.name,
                    data_type=definition.data_type.value,
                    spatial_granularity=definition.spatial_granularity,
                    responsibility_unit=definition.responsibility_unit,
                    update_interval_days=definition.update_interval_days,
                    is_core=definition.is_core,
                    contract=_asset_contract(definition),
                    created_at=now,
                    updated_at=now,
                )
                session.add(asset)
                await session.flush()
            version = DataAssetVersion(
                asset_id=asset.id,
                version=f"compressed-raster-{uuid4()}",
                status="imported",
                source_uri="https://example.gov.invalid/compressed.tif",
                schema_summary={},
                record_count=0,
                spatial_extent=None,
                source_crs="EPSG:4326",
                checksum=source_checksum,
                managed_path=None,
                imported_by=FIXTURE_ACTOR,
                imported_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(version)
            await session.flush()
            version_id = version.id
    yield SeededImportedVersion(version_id, source_path, definition)
    await _cleanup_fixture_data(session_factory)


def _manifest(descriptor):
    return {
        "band_count": descriptor.band_count,
        "dtype": descriptor.dtype,
        "nodata": descriptor.nodata,
        "resolution_x": descriptor.resolution_x,
        "resolution_y": descriptor.resolution_y,
    }


async def test_save_raster_version_can_read_postgis_metadata(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            raster_id = await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            row = (
                await session.execute(
                    text(
                        """
                        SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast),
                               checksum, band_manifest
                        FROM data_asset_rasters
                        WHERE id = :raster_id
                        """
                    ),
                    {"raster_id": raster_id},
                )
            ).one()
    assert row[0] == descriptor.width
    assert row[1] == descriptor.height
    assert row[2] == descriptor.srid
    assert len(row[3]) == 64


async def test_save_raster_version_sets_numeric_esri_authority(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    import numpy as np
    import rasterio
    from rasterio.transform import Affine

    with rasterio.open(
        source.source_path,
        "w",
        driver="GTiff",
        width=4,
        height=5,
        count=1,
        dtype="float32",
        crs="ESRI:102025",
        nodata=-3.4028230607370965e38,
        transform=Affine(1000, 0, 0, 0, -1000, 0),
    ) as dataset:
        dataset.write(np.ones((5, 4), dtype="float32"), 1)
    checksum = hashlib.sha256(source.source_path.read_bytes()).hexdigest()
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(DataAssetVersion)
                .where(DataAssetVersion.id == source.version_id)
                .values(checksum=checksum)
            )
            raster_id = await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            stored_srid = (
                await session.execute(
                    text(
                        """
                        SELECT ST_SRID(rast)
                        FROM data_asset_rasters
                        WHERE id = :raster_id
                        """
                    ),
                    {"raster_id": raster_id},
                )
            ).scalar_one()

    assert descriptor.srid == 102025
    assert stored_srid == 102025


async def test_save_raster_version_rejects_version_checksum_mismatch(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(DataAssetVersion)
                .where(DataAssetVersion.id == source.version_id)
                .values(checksum="b" * 64)
            )
            with pytest.raises(ValueError, match="checksum"):
                await save_raster_version(
                    session,
                    source.version_id,
                    source.source_path,
                    descriptor,
                )


async def test_save_raster_version_rejects_persisted_metadata_mismatch(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            raster_id = await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            await session.execute(
                text("UPDATE data_asset_rasters SET width = width + 1 WHERE id = :id"),
                {"id": raster_id},
            )
            with pytest.raises(RuntimeError, match="metadata"):
                await _verify_saved_raster(
                    session,
                    raster_id,
                    descriptor,
                )


async def test_load_raster_version_round_trips_payload_and_manifest(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            payload, manifest = await load_raster_version(
                session,
                source.version_id,
            )
            stored_checksum = (
                await session.execute(
                    text(
                        """
                        SELECT checksum
                        FROM data_asset_rasters
                        WHERE version_id = :version_id
                        """
                    ),
                    {"version_id": source.version_id},
                )
            ).scalar_one()
    assert hashlib.sha256(payload).hexdigest() == stored_checksum
    assert manifest == _manifest(descriptor)


async def test_load_raster_version_accepts_reencoded_compressed_geotiff(
    session_factory,
    seeded_compressed_imported_version,
) -> None:
    source = seeded_compressed_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            stored_checksum = (
                await session.execute(
                    text(
                        """
                        SELECT checksum
                        FROM data_asset_rasters
                        WHERE version_id = :version_id
                        """
                    ),
                    {"version_id": source.version_id},
                )
            ).scalar_one()
            payload, manifest = await load_raster_version(
                session,
                source.version_id,
            )
    assert hashlib.sha256(payload).hexdigest() == stored_checksum
    assert manifest == _manifest(descriptor)
    assert payload != source.source_path.read_bytes()


async def test_load_raster_version_rejects_not_found(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(LookupError, match="not found"):
            await load_raster_version(session, uuid4())


async def test_load_raster_version_rejects_checksum_mismatch(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            raster_id = await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            await session.execute(
                text("UPDATE data_asset_rasters SET checksum = :checksum WHERE id = :id"),
                {"checksum": "c" * 64, "id": raster_id},
            )
            with pytest.raises(RuntimeError, match="checksum"):
                await load_raster_version(session, source.version_id)


async def test_load_raster_version_rejects_null_payload() -> None:
    row = SimpleNamespace(payload=None, checksum="a" * 64, band_manifest={})
    result = SimpleNamespace(one_or_none=lambda: row)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    with pytest.raises(RuntimeError, match="payload"):
        await load_raster_version(session, uuid4())
