from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import numpy as np
from pyproj import CRS
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intensity.artifacts import RasterCodec
from app.intensity.domain import (
    GridDefinition,
    ProductStatus,
    ProductType,
)
from app.intensity.grid import grid_definition_from_bounds
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.region import RegionProfile
from app.regions.models import RegionBoundary


@dataclass(frozen=True, slots=True)
class IntensityProductWrite:
    run_id: UUID
    task_id: UUID
    product_type: ProductType
    status: ProductStatus
    algorithm_version: str
    parameter_version: str
    strategy_version: str | None
    grid_definition: GridDefinition
    region_profile_version: str
    input_fingerprint: str
    input_checksum: str
    quality_grade: str | None
    coverage_ratio: float
    statistics: dict
    source_product_id: str | None
    observed_at: datetime | None
    bands: list[tuple[str, np.ndarray]]


@dataclass(frozen=True, slots=True)
class ProductArrays:
    product_id: UUID
    product_type: str
    status: str
    bands: dict[str, np.ndarray]
    statistics: dict


class IntensityRepository:
    async def resolve_grid_definition(
        self,
        session: AsyncSession,
        profile: RegionProfile,
        boundary_version: str,
    ) -> GridDefinition:
        boundary = await session.scalar(
            select(RegionBoundary).where(RegionBoundary.version == boundary_version)
        )
        if boundary is None:
            raise LookupError("region boundary version not found")
        target_srid = CRS.from_user_input(profile.grid_crs).to_epsg()
        if target_srid is None:
            raise ValueError("region profile grid CRS must map to an EPSG code")
        transformed = func.ST_Transform(RegionBoundary.geom, target_srid)
        buffered = func.ST_Buffer(transformed, profile.grid_buffer_km * 1000)
        envelope = func.ST_Envelope(buffered)
        row = (
            await session.execute(
                select(
                    func.ST_XMin(envelope).label("min_x"),
                    func.ST_YMin(envelope).label("min_y"),
                    func.ST_XMax(envelope).label("max_x"),
                    func.ST_YMax(envelope).label("max_y"),
                ).where(RegionBoundary.id == boundary.id)
            )
        ).one()
        provisional = grid_definition_from_bounds(
            min_x=float(row.min_x),
            min_y=float(row.min_y),
            max_x=float(row.max_x),
            max_y=float(row.max_y),
            resolution_m=profile.grid_resolution_m,
            crs=profile.grid_crs,
            version="pending",
        )
        identity = {
            "region_profile_version": profile.version,
            "boundary_version": boundary_version,
            "min_x": provisional.origin_x,
            "min_y": provisional.origin_y - provisional.height * provisional.resolution_m,
            "max_x": provisional.origin_x + provisional.width * provisional.resolution_m,
            "max_y": provisional.origin_y,
            "resolution_m": provisional.resolution_m,
            "crs": provisional.crs,
        }
        checksum = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return GridDefinition(
            version=f"{profile.version}:{boundary.version}:{checksum[:16]}",
            crs=provisional.crs,
            resolution_m=provisional.resolution_m,
            origin_x=provisional.origin_x,
            origin_y=provisional.origin_y,
            width=provisional.width,
            height=provisional.height,
        )

    async def load_product_arrays(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        product_type: ProductType,
    ) -> ProductArrays | None:
        product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == product_type.value,
            )
        )
        if product is None:
            return None
        if product.status not in {
            ProductStatus.AVAILABLE.value,
            ProductStatus.PARTIAL.value,
        }:
            return ProductArrays(
                product_id=product.id,
                product_type=product.product_type,
                status=product.status,
                bands={},
                statistics=dict(product.statistics),
            )
        raster_bands, manifest = await self.load_raster(session, product.id)
        names = [item["name"] for item in manifest.get("bands", [])]
        if len(names) != len(raster_bands):
            raise ValueError("raster band manifest does not match raster")
        return ProductArrays(
            product_id=product.id,
            product_type=product.product_type,
            status=product.status,
            bands=dict(zip(names, raster_bands, strict=True)),
            statistics=dict(product.statistics),
        )

    async def save_product(
        self,
        session: AsyncSession,
        write: IntensityProductWrite,
    ) -> UUID:
        existing = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == write.run_id,
                IntensityFieldProduct.product_type == write.product_type.value,
            )
        )
        now = datetime.now(UTC)
        if existing is not None:
            raster_required = write.status in {
                ProductStatus.AVAILABLE,
                ProductStatus.PARTIAL,
            }
            if raster_required:
                raster_id = await session.scalar(
                    select(IntensityRaster.id)
                    .where(IntensityRaster.product_id == existing.id)
                    .limit(1)
                )
                if (
                    existing.completed_at is None
                    or existing.output_checksum is None
                    or raster_id is None
                ):
                    raise ValueError("existing intensity product is incomplete")
            elif existing.completed_at is None:
                existing.completed_at = now
            if existing.status != write.status.value:
                raise ValueError("published product status cannot be overwritten")
            if existing.algorithm_version != write.algorithm_version:
                raise ValueError("published product algorithm version cannot be overwritten")
            if existing.input_fingerprint != write.input_fingerprint:
                raise ValueError("published product fingerprint cannot be overwritten")
            return existing.id

        if not write.bands and write.status in {
            ProductStatus.AVAILABLE,
            ProductStatus.PARTIAL,
        }:
            raise ValueError("available or partial intensity product requires raster bands")

        product_id = uuid4()
        product = IntensityFieldProduct(
            id=product_id,
            run_id=write.run_id,
            task_id=write.task_id,
            product_type=write.product_type.value,
            status=write.status.value,
            algorithm_version=write.algorithm_version,
            parameter_version=write.parameter_version,
            strategy_version=write.strategy_version,
            grid_definition_version=write.grid_definition.version,
            region_profile_version=write.region_profile_version,
            input_fingerprint=write.input_fingerprint,
            input_checksum=write.input_checksum,
            output_checksum=None,
            quality_grade=write.quality_grade,
            coverage_ratio=write.coverage_ratio,
            spatial_extent=None,
            statistics=write.statistics,
            source_product_id=write.source_product_id,
            observed_at=write.observed_at,
            created_at=now,
            completed_at=None,
            published_at=None,
        )
        session.add(product)
        await session.flush()

        if write.bands:
            manifest = {
                "bands": [
                    {"number": index, "name": name}
                    for index, (name, _) in enumerate(write.bands, start=1)
                ]
            }
            payload = RasterCodec.encode(
                write.grid_definition,
                write.bands,
                manifest,
            )
            checksum = RasterCodec.checksum(payload)
            srid = CRS.from_user_input(write.grid_definition.crs).to_epsg()
            if srid is None:
                raise ValueError("grid CRS must map to an EPSG code")

            raster_expression = _raster_expression(len(write.bands))
            bindings = {
                "id": uuid4(),
                "product_id": product_id,
                "manifest": json.dumps(manifest),
                "checksum": checksum,
                "width": write.grid_definition.width,
                "height": write.grid_definition.height,
                "upperleftx": write.grid_definition.origin_x,
                "upperlefty": write.grid_definition.origin_y,
                "scalex": float(write.grid_definition.resolution_m),
                "scaley": -float(write.grid_definition.resolution_m),
                "srid": srid,
                "created_at": now,
            }
            for index, (_, values) in enumerate(write.bands, start=1):
                bindings[f"values_{index}"] = np.asarray(
                    values,
                    dtype=np.float64,
                ).tolist()

            await session.execute(
                text(
                    f"""
                    INSERT INTO intensity_rasters (
                        id,
                        product_id,
                        rast,
                        band_manifest,
                        checksum,
                        width,
                        height,
                        srid,
                        created_at
                    )
                    VALUES (
                        :id,
                        :product_id,
                        {raster_expression},
                        CAST(:manifest AS jsonb),
                        :checksum,
                        :width,
                        :height,
                        :srid,
                        :created_at
                    )
                    """
                ),
                bindings,
            )
            persisted_values = await _dump_band_values(
                session,
                product_id,
                expected_count=len(write.bands),
            )
            persisted_bands = [
                (name, values)
                for (name, _), values in zip(write.bands, persisted_values)
            ]
            reconstructed_payload = RasterCodec.encode(
                write.grid_definition,
                persisted_bands,
                manifest,
            )
            if RasterCodec.checksum(reconstructed_payload) != checksum:
                raise RuntimeError("intensity raster checksum verification failed")

            await session.execute(
                text(
                    """
                    UPDATE intensity_field_products
                    SET output_checksum = :checksum,
                        completed_at = :now,
                        spatial_extent = (
                            SELECT ST_Transform(ST_Envelope(r.rast), 4326)
                            FROM intensity_rasters r
                            WHERE r.product_id = :product_id
                        )
                    WHERE id = :product_id
                    """
                ),
                {
                    "checksum": checksum,
                    "now": now,
                    "product_id": product_id,
                },
            )
            product.output_checksum = checksum
            product.completed_at = now
        else:
            product.completed_at = now

        return product_id

    async def get_product(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        product_type: ProductType,
    ) -> dict | None:
        row = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run_id,
                IntensityFieldProduct.product_type == product_type.value,
            )
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "product_type": row.product_type,
            "status": row.status,
            "quality_grade": row.quality_grade,
            "coverage_ratio": float(row.coverage_ratio),
            "output_checksum": row.output_checksum,
            "statistics": dict(row.statistics),
        }

    async def list_products(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> list[dict]:
        products = (
            await session.scalars(
                select(IntensityFieldProduct)
                .where(IntensityFieldProduct.run_id == run_id)
                .order_by(IntensityFieldProduct.product_type)
            )
        ).all()
        return [
            {
                "product_id": str(product.id),
                "product_type": product.product_type,
                "status": product.status,
                "algorithm_version": product.algorithm_version,
                "parameter_version": product.parameter_version,
                "strategy_version": product.strategy_version,
                "grid_definition_version": product.grid_definition_version,
                "region_profile_version": product.region_profile_version,
                "quality_grade": product.quality_grade,
                "coverage_ratio": float(product.coverage_ratio),
                "output_checksum": product.output_checksum,
                "source_product_id": product.source_product_id,
                "observed_at": product.observed_at,
                "completed_at": product.completed_at,
                "statistics": dict(product.statistics),
            }
            for product in products
        ]

    async def load_raster(
        self,
        session: AsyncSession,
        product_id: UUID,
    ) -> tuple[list[np.ndarray], dict]:
        product = await session.get(IntensityFieldProduct, product_id)
        if product is None:
            raise LookupError("intensity product not found")

        raster_metadata = (
            await session.execute(
                text(
                    """
                    SELECT
                        ST_Width(rast),
                        ST_Height(rast),
                        ST_SRID(rast),
                        ST_ScaleX(rast),
                        ST_ScaleY(rast),
                        ST_UpperLeftX(rast),
                        ST_UpperLeftY(rast)
                    FROM intensity_rasters
                    WHERE product_id = :product_id
                    """
                ),
                {"product_id": product_id},
            )
        ).one_or_none()
        if raster_metadata is None:
            raise LookupError("intensity raster not found")

        (
            width,
            height,
            srid,
            scale_x,
            scale_y,
            origin_x,
            origin_y,
        ) = raster_metadata
        raw_bands = await _dump_band_values(session, product_id)
        if not raw_bands:
            raise LookupError("intensity raster has no bands")

        bands = [
            np.asarray(values, dtype=np.float64)
            for values in raw_bands
        ]
        metadata = {
            "crs": CRS.from_epsg(srid).to_string() if srid else None,
            "width": int(width),
            "height": int(height),
            "srid": int(srid),
            "resolution_m": abs(float(scale_x)),
            "origin_x": float(origin_x),
            "origin_y": float(origin_y),
            "grid_definition_version": product.grid_definition_version,
        }

        manifest = await session.scalar(
            text(
                """
                SELECT band_manifest
                FROM intensity_rasters
                WHERE product_id = :product_id
                """
            ),
            {"product_id": product_id},
        )
        if manifest is not None:
            metadata["bands"] = manifest["bands"]
        return bands, metadata


async def _dump_band_values(
    session: AsyncSession,
    product_id: UUID,
    *,
    expected_count: int | None = None,
) -> list[np.ndarray]:
    values = (
        await session.scalars(
            text(
                """
                SELECT ST_DumpValues(rast, band_number, false)
                FROM intensity_rasters
                CROSS JOIN LATERAL generate_series(
                    1,
                    ST_NumBands(rast)
                ) AS band_number
                WHERE product_id = :product_id
                ORDER BY band_number
                """
            ),
            {"product_id": product_id},
        )
    ).all()
    if expected_count is not None and len(values) != expected_count:
        raise RuntimeError("intensity raster band count verification failed")
    return [np.asarray(value, dtype=np.float64) for value in values]


def _raster_expression(band_count: int) -> str:
    expression = (
        "ST_MakeEmptyRaster("
        ":width, :height, :upperleftx, :upperlefty, "
        ":scalex, :scaley, 0, 0, :srid"
        ")"
    )
    for band_number in range(1, band_count + 1):
        expression = (
            f"ST_SetValues("
            f"ST_AddBand({expression}, {band_number}, '64BF', 0, NULL), "
            f"{band_number}, 1, 1, :values_{band_number}"
            f")"
        )
    return expression
