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
    checksum = hashlib.sha256(source.source_path.read_bytes()).hexdigest()
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
                    checksum,
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
    assert payload == source.source_path.read_bytes()
    assert manifest == _manifest(descriptor)


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
