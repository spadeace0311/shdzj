import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_assets.domain import NormalizedRasterData


async def save_raster_version(
    session: AsyncSession,
    version_id: UUID,
    path: Path,
    descriptor: NormalizedRasterData,
) -> UUID:
    payload = path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    raster_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO data_asset_rasters (
                id, version_id, rast, band_manifest, checksum,
                width, height, srid, spatial_extent, created_at
            )
            VALUES (
                :id, :version_id, ST_FromGDALRaster(:payload),
                CAST(:manifest AS jsonb), :checksum,
                :width, :height, :srid,
                ST_Transform(
                    ST_Envelope(ST_FromGDALRaster(:payload))::geometry,
                    4326
                ),
                :created_at
            )
            """
        ),
        {
            "id": raster_id,
            "version_id": version_id,
            "payload": payload,
            "manifest": json.dumps(
                {
                    "band_count": descriptor.band_count,
                    "dtype": descriptor.dtype,
                    "nodata": descriptor.nodata,
                    "resolution_x": descriptor.resolution_x,
                    "resolution_y": descriptor.resolution_y,
                },
                sort_keys=True,
            ),
            "checksum": checksum,
            "width": descriptor.width,
            "height": descriptor.height,
            "srid": descriptor.srid,
            "created_at": datetime.now(UTC),
        },
    )
    row = (
        await session.execute(
            text(
                """
                SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast)
                FROM data_asset_rasters
                WHERE id = :id
                """
            ),
            {"id": raster_id},
        )
    ).one()
    if (int(row[0]), int(row[1]), int(row[2])) != (
        descriptor.width,
        descriptor.height,
        descriptor.srid,
    ):
        raise RuntimeError("persisted raster metadata does not match source")
    return raster_id


async def load_raster_version(
    session: AsyncSession,
    version_id: UUID,
) -> tuple[bytes, dict]:
    row = (
        await session.execute(
            text(
                """
                SELECT ST_AsGDALRaster(rast, 'GTiff') AS payload,
                       checksum, band_manifest
                FROM data_asset_rasters
                WHERE version_id = :version_id
                """
            ),
            {"version_id": version_id},
        )
    ).one_or_none()
    if row is None:
        raise LookupError("data asset raster not found")
    return bytes(row.payload), dict(row.band_manifest)
