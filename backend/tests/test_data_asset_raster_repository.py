from sqlalchemy import text

from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.raster_repository import save_raster_version


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
