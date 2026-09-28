import hashlib
import json
from datetime import UTC, datetime
from math import hypot, isclose, isfinite
from pathlib import Path
from uuid import UUID, uuid4

from shapely import wkt as shapely_wkt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_assets.domain import NormalizedRasterData
from app.data_assets.models import DataAssetVersion


_PIXEL_TYPE_BY_DTYPE = {
    "uint8": "8BUI",
    "uint16": "16BUI",
    "int16": "16BSI",
    "uint32": "32BUI",
    "int32": "32BSI",
    "float32": "32BF",
    "float64": "64BF",
    "int8": "8BSI",
}


def _band_manifest(descriptor: NormalizedRasterData) -> dict:
    return {
        "band_count": descriptor.band_count,
        "dtype": descriptor.dtype,
        "nodata": descriptor.nodata,
        "resolution_x": descriptor.resolution_x,
        "resolution_y": descriptor.resolution_y,
    }


def _validate_descriptor(descriptor: NormalizedRasterData) -> None:
    if descriptor.width <= 0 or descriptor.height <= 0 or descriptor.band_count <= 0:
        raise ValueError("raster dimensions and band count must be positive")
    if (
        not isfinite(descriptor.resolution_x)
        or descriptor.resolution_x <= 0
        or not isfinite(descriptor.resolution_y)
        or descriptor.resolution_y <= 0
    ):
        raise ValueError("raster resolution is invalid")
    if descriptor.nodata is not None and not isfinite(float(descriptor.nodata)):
        raise ValueError("raster nodata must be finite")
    extent = descriptor.spatial_extent
    if len(extent) != 4 or not all(isfinite(float(value)) for value in extent):
        raise ValueError("raster bounds are invalid")
    if extent[0] >= extent[2] or extent[1] >= extent[3]:
        raise ValueError("raster bounds are invalid")


async def _ensure_version_checksum(
    session: AsyncSession,
    version_id: UUID,
    checksum: str,
) -> None:
    stored = await session.scalar(
        select(DataAssetVersion.checksum).where(DataAssetVersion.id == version_id)
    )
    if stored is None:
        raise LookupError("data asset version not found")
    if stored != checksum:
        raise ValueError("version checksum does not match raster payload")


def _bounds_match(left_wkt: str, right_wkt: str) -> bool:
    left = shapely_wkt.loads(left_wkt)
    right = shapely_wkt.loads(right_wkt)
    return all(
        isclose(
            float(left_value),
            float(right_value),
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        for left_value, right_value in zip(left.bounds, right.bounds)
    )


async def _raster_payload_checksum(
    session: AsyncSession,
    raster_id: UUID,
) -> tuple[bytes, str]:
    payload = (
        await session.execute(
            text(
                """
                SELECT ST_AsGDALRaster(rast, 'GTiff')
                FROM data_asset_rasters
                WHERE id = :id
                """
            ),
            {"id": raster_id},
        )
    ).scalar_one()
    if payload is None:
        raise RuntimeError("data asset raster payload is null")
    payload_bytes = bytes(payload)
    return payload_bytes, hashlib.sha256(payload_bytes).hexdigest()


async def _verify_saved_raster(
    session: AsyncSession,
    raster_id: UUID,
    descriptor: NormalizedRasterData,
) -> None:
    expected_manifest = _band_manifest(descriptor)
    row = (
        await session.execute(
            text(
                """
                SELECT ST_Metadata(rast),
                       ST_AsText(ST_Transform(ST_Envelope(rast)::geometry, 4326)),
                       ST_AsText(spatial_extent),
                       band_manifest,
                       checksum,
                       ST_AsText(
                           ST_Transform(
                               ST_MakeEnvelope(
                                   :min_x, :min_y, :max_x, :max_y, :srid
                               ),
                               4326
                           )
                       ),
                       width,
                       height,
                       srid
                FROM data_asset_rasters
                WHERE id = :id
                """
            ),
            {
                "id": raster_id,
                "min_x": descriptor.spatial_extent[0],
                "min_y": descriptor.spatial_extent[1],
                "max_x": descriptor.spatial_extent[2],
                "max_y": descriptor.spatial_extent[3],
                "srid": descriptor.srid,
            },
        )
    ).one()
    metadata = row[0]
    actual_extent = row[1]
    stored_extent = row[2]
    stored_manifest = row[3]
    stored_checksum = row[4]
    expected_extent = row[5]
    stored_width = int(row[6])
    stored_height = int(row[7])
    stored_srid = int(row[8])
    persisted_width = int(metadata[2])
    persisted_height = int(metadata[3])
    persisted_scale_x = float(metadata[4])
    persisted_scale_y = float(metadata[5])
    persisted_skew_x = float(metadata[6])
    persisted_skew_y = float(metadata[7])
    persisted_srid = int(metadata[8])
    persisted_band_count = int(metadata[9])
    persisted_resolution_x = hypot(persisted_scale_x, persisted_skew_y)
    persisted_resolution_y = hypot(persisted_skew_x, persisted_scale_y)
    _, exported_checksum = await _raster_payload_checksum(session, raster_id)

    if (
        persisted_width != descriptor.width
        or persisted_height != descriptor.height
        or persisted_srid != descriptor.srid
        or persisted_band_count != descriptor.band_count
        or stored_width != descriptor.width
        or stored_height != descriptor.height
        or stored_srid != descriptor.srid
        or not isclose(
            persisted_resolution_x,
            descriptor.resolution_x,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        or not isclose(
            persisted_resolution_y,
            descriptor.resolution_y,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        or stored_checksum != exported_checksum
        or dict(stored_manifest) != expected_manifest
        or not _bounds_match(actual_extent, expected_extent)
        or not _bounds_match(stored_extent, expected_extent)
    ):
        raise RuntimeError("persisted raster metadata does not match source")

    for band_number in range(1, descriptor.band_count + 1):
        band_row = (
            await session.execute(
                text(
                    """
                    SELECT ST_BandPixelType(rast, :band),
                           ST_BandNoDataValue(rast, :band)
                    FROM data_asset_rasters
                    WHERE id = :id
                    """
                ),
                {"id": raster_id, "band": band_number},
            )
        ).one()
        pixel_type = band_row[0]
        nodata = band_row[1]
        expected_pixel_type = _PIXEL_TYPE_BY_DTYPE.get(descriptor.dtype)
        if pixel_type != expected_pixel_type:
            raise RuntimeError("persisted raster metadata does not match source")
        if descriptor.nodata is None:
            if nodata is not None:
                raise RuntimeError("persisted raster metadata does not match source")
        elif nodata is None or not isclose(
            float(nodata),
            float(descriptor.nodata),
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            raise RuntimeError("persisted raster metadata does not match source")


async def save_raster_version(
    session: AsyncSession,
    version_id: UUID,
    path: Path,
    descriptor: NormalizedRasterData,
) -> UUID:
    payload = path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    _validate_descriptor(descriptor)
    await _ensure_version_checksum(session, version_id, checksum)
    raster_id = uuid4()
    manifest = json.dumps(
        _band_manifest(descriptor),
        sort_keys=True,
        allow_nan=False,
    )
    await session.execute(
        text(
            """
            WITH source AS (
                SELECT CAST(:id AS uuid) AS id,
                       CAST(:version_id AS uuid) AS version_id,
                       ST_FromGDALRaster(:payload) AS rast
            )
            INSERT INTO data_asset_rasters (
                id, version_id, rast, band_manifest, checksum,
                width, height, srid, spatial_extent, created_at
            )
            SELECT source.id,
                   source.version_id,
                   source.rast,
                   CAST(:manifest AS jsonb),
                   :checksum,
                   :width,
                   :height,
                   :srid,
                   ST_Transform(ST_Envelope(source.rast)::geometry, 4326),
                   :created_at
            FROM source
            """
        ),
        {
            "id": raster_id,
            "version_id": version_id,
            "payload": payload,
            "manifest": manifest,
            "checksum": checksum,
            "width": descriptor.width,
            "height": descriptor.height,
            "srid": descriptor.srid,
            "created_at": datetime.now(UTC),
        },
    )
    _, canonical_checksum = await _raster_payload_checksum(session, raster_id)
    await session.execute(
        text(
            """
            UPDATE data_asset_rasters
            SET checksum = :checksum
            WHERE id = :id
            """
        ),
        {"id": raster_id, "checksum": canonical_checksum},
    )
    await _verify_saved_raster(session, raster_id, descriptor)
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
    if row.payload is None:
        raise RuntimeError("data asset raster payload is null")
    payload = bytes(row.payload)
    if hashlib.sha256(payload).hexdigest() != row.checksum:
        raise RuntimeError("raster checksum does not match persisted checksum")
    if row.band_manifest is None:
        raise RuntimeError("data asset raster band manifest is null")
    return payload, dict(row.band_manifest)
