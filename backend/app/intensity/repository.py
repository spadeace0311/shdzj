from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
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
    band_metadata: dict[str, dict] = field(default_factory=dict)


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
        normalized_bands = _normalized_bands(write)
        _validate_product_write(write, normalized_bands)
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
                raise ValueError("existing intensity product is incomplete")
            _validate_existing_product(write, existing)
            if raster_required:
                await _load_and_verify_raster(
                    session,
                    existing.id,
                )
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

        if normalized_bands:
            manifest = _build_band_manifest(
                write.grid_definition,
                normalized_bands,
                write.band_metadata,
            )
            checksum = RasterCodec.content_checksum(
                write.grid_definition,
                normalized_bands,
                manifest,
            )
            srid = CRS.from_user_input(write.grid_definition.crs).to_epsg()
            if srid is None:
                raise ValueError("grid CRS must map to an EPSG code")

            raster_expression = _raster_expression(len(normalized_bands))
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
            for index, (_, values) in enumerate(normalized_bands, start=1):
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
            await _load_and_verify_raster(
                session,
                product_id,
                expected_definition=write.grid_definition,
                expected_bands=normalized_bands,
                expected_checksum=checksum,
            )

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
                "published_at": product.published_at,
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
        bands, metadata = await _load_and_verify_raster(
            session,
            product_id,
        )
        if product.output_checksum != metadata["checksum"]:
            raise RuntimeError("persisted raster checksum does not match product")
        return bands, metadata


def _normalized_bands(
    write: IntensityProductWrite,
) -> list[tuple[str, np.ndarray]]:
    normalized: list[tuple[str, np.ndarray]] = []
    expected = (
        write.grid_definition.height,
        write.grid_definition.width,
    )
    for name, values in write.bands:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("raster band names must not be empty")
        array = np.asarray(values, dtype=np.float64)
        if array.shape != expected:
            raise ValueError("raster band shape does not match grid definition")
        normalized.append((name, np.ascontiguousarray(array)))
    if len({name for name, _ in normalized}) != len(normalized):
        raise ValueError("raster band names must be unique")
    return normalized


def _validate_existing_product(
    write: IntensityProductWrite,
    existing: IntensityFieldProduct,
) -> None:
    immutable_values = {
        "status": existing.status,
        "algorithm_version": existing.algorithm_version,
        "parameter_version": existing.parameter_version,
        "strategy_version": existing.strategy_version,
        "grid_definition_version": existing.grid_definition_version,
        "region_profile_version": existing.region_profile_version,
        "input_fingerprint": existing.input_fingerprint,
        "input_checksum": existing.input_checksum,
        "quality_grade": existing.quality_grade,
        "coverage_ratio": float(existing.coverage_ratio),
        "source_product_id": existing.source_product_id,
        "observed_at": existing.observed_at,
        "statistics": dict(existing.statistics),
    }
    incoming_values = {
        "status": write.status.value,
        "algorithm_version": write.algorithm_version,
        "parameter_version": write.parameter_version,
        "strategy_version": write.strategy_version,
        "grid_definition_version": write.grid_definition.version,
        "region_profile_version": write.region_profile_version,
        "input_fingerprint": write.input_fingerprint,
        "input_checksum": write.input_checksum,
        "quality_grade": write.quality_grade,
        "coverage_ratio": float(write.coverage_ratio),
        "source_product_id": write.source_product_id,
        "observed_at": write.observed_at,
        "statistics": write.statistics,
    }
    for key in immutable_values:
        if immutable_values[key] != incoming_values[key]:
            raise ValueError(f"published intensity product {key} cannot be overwritten")


def _validate_product_write(
    write: IntensityProductWrite,
    normalized_bands: list[tuple[str, np.ndarray]],
) -> None:
    if not math.isfinite(float(write.coverage_ratio)) or not 0.0 <= float(
        write.coverage_ratio
    ) <= 1.0:
        raise ValueError("intensity product coverage ratio must be between zero and one")
    if (
        write.status in {ProductStatus.AVAILABLE, ProductStatus.PARTIAL}
        and not normalized_bands
    ):
        raise ValueError("available or partial intensity product requires raster bands")
    if (
        write.status not in {ProductStatus.AVAILABLE, ProductStatus.PARTIAL}
        and normalized_bands
    ):
        raise ValueError("terminal intensity product must not persist raster bands")
    _validate_json_numbers(write.statistics)


def _validate_json_numbers(value: object) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _validate_json_numbers(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_numbers(item)
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("intensity product metadata must contain finite numbers")


def _build_band_manifest(
    definition: GridDefinition,
    bands: list[tuple[str, np.ndarray]],
    band_metadata: dict[str, dict],
) -> dict:
    entries = []
    for index, (name, values) in enumerate(bands, start=1):
        metadata = dict(band_metadata.get(name, {}))
        band_type = metadata.get("type", "float64")
        if not isinstance(band_type, str) or not band_type:
            raise ValueError(f"raster band {name} type must not be empty")
        unit = metadata.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise ValueError(f"raster band {name} unit must be a string or null")
        nodata = metadata.get("nodata")
        if nodata is not None:
            nodata = _finite_float_value(
                nodata,
                f"raster band {name} nodata",
            )
        scale = _finite_float_value(
            metadata.get("scale", 1.0),
            f"raster band {name} scale",
        )
        if scale == 0:
            raise ValueError(f"raster band {name} scale must not be zero")
        entries.append(
            {
                "number": index,
                "name": name,
                "type": band_type,
                "unit": unit,
                "nodata": nodata,
                "scale": scale,
                "checksum": _array_checksum(values),
            }
        )
    return {
        "grid": {
            "version": definition.version,
            "crs": definition.crs,
            "resolution_m": definition.resolution_m,
            "origin_x": definition.origin_x,
            "origin_y": definition.origin_y,
            "width": definition.width,
            "height": definition.height,
        },
        "bands": entries,
    }


def _finite_float_value(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{field} must be finite")
    return converted


def _array_checksum(values: np.ndarray) -> str:
    array = np.ascontiguousarray(values, dtype=np.float64)
    digest = hashlib.sha256(b"intensity-band-v1\0")
    digest.update(str((array.shape[0], array.shape[1])).encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def _grid_definition_from_manifest(manifest: dict) -> GridDefinition:
    grid = manifest.get("grid")
    if not isinstance(grid, dict):
        raise RuntimeError("persisted raster manifest is missing grid metadata")
    try:
        return GridDefinition(
            version=str(grid["version"]),
            crs=str(grid["crs"]),
            resolution_m=int(grid["resolution_m"]),
            origin_x=float(grid["origin_x"]),
            origin_y=float(grid["origin_y"]),
            width=int(grid["width"]),
            height=int(grid["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("persisted raster grid metadata is invalid") from exc


def _manifest_bands(manifest: dict) -> list[dict]:
    bands = manifest.get("bands")
    if not isinstance(bands, list) or not bands:
        raise RuntimeError("persisted raster manifest has no bands")
    for band in bands:
        if not isinstance(band, dict) or {"number", "name"} - set(band):
            raise RuntimeError("persisted raster band manifest is invalid")
    return bands


def _expected_grid_srid(definition: GridDefinition) -> int:
    srid = CRS.from_user_input(definition.crs).to_epsg()
    if srid is None:
        raise ValueError("grid CRS must map to an EPSG code")
    return srid


def _check_float_equal(
    actual: object,
    expected: float,
    field_name: str,
) -> None:
    if not math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-9):
        raise RuntimeError(f"persisted raster {field_name} verification failed")


async def _load_and_verify_raster(
    session: AsyncSession,
    product_id: UUID,
    *,
    expected_definition: GridDefinition | None = None,
    expected_bands: list[tuple[str, np.ndarray]] | None = None,
    expected_checksum: str | None = None,
) -> tuple[list[np.ndarray], dict]:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    ST_Width(rast) AS actual_width,
                    ST_Height(rast) AS actual_height,
                    ST_SRID(rast) AS actual_srid,
                    ST_ScaleX(rast) AS scale_x,
                    ST_ScaleY(rast) AS scale_y,
                    ST_UpperLeftX(rast) AS origin_x,
                    ST_UpperLeftY(rast) AS origin_y,
                    band_manifest,
                    checksum,
                    width AS stored_width,
                    height AS stored_height,
                    srid AS stored_srid
                FROM intensity_rasters
                WHERE product_id = :product_id
                """
            ),
            {"product_id": product_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("intensity raster not found")

    manifest = row["band_manifest"]
    if not isinstance(manifest, dict):
        raise RuntimeError("persisted raster manifest is not a mapping")
    manifest = dict(manifest)
    definition = expected_definition or _grid_definition_from_manifest(manifest)
    if expected_definition is None:
        expected_bands = None
    if expected_definition is None and expected_checksum is None:
        expected_checksum = None

    decoded_bands = await _dump_band_values(session, product_id)
    manifest_bands = _manifest_bands(manifest)
    actual_width = int(row["actual_width"])
    actual_height = int(row["actual_height"])
    actual_srid = int(row["actual_srid"])
    stored_width = int(row["stored_width"])
    stored_height = int(row["stored_height"])
    stored_srid = int(row["stored_srid"])
    scale_x = float(row["scale_x"])
    scale_y = float(row["scale_y"])
    origin_x = float(row["origin_x"])
    origin_y = float(row["origin_y"])

    if actual_width != definition.width or actual_height != definition.height:
        raise RuntimeError("persisted raster dimensions verification failed")
    if stored_width != actual_width or stored_height != actual_height:
        raise RuntimeError("persisted raster dimension metadata verification failed")
    expected_srid = _expected_grid_srid(definition)
    if actual_srid != expected_srid or stored_srid != actual_srid:
        raise RuntimeError("persisted raster SRID verification failed")
    _check_float_equal(scale_x, float(definition.resolution_m), "scale x")
    _check_float_equal(scale_y, -float(definition.resolution_m), "scale y")
    _check_float_equal(origin_x, float(definition.origin_x), "origin x")
    _check_float_equal(origin_y, float(definition.origin_y), "origin y")
    if str(manifest["grid"]["version"]) != definition.version:
        raise RuntimeError("persisted raster grid version verification failed")
    if len(decoded_bands) != len(manifest_bands):
        raise RuntimeError("persisted raster band count verification failed")

    for index, (band_meta, values) in enumerate(
        zip(manifest_bands, decoded_bands, strict=True),
        start=1,
    ):
        if int(band_meta["number"]) != index:
            raise RuntimeError("persisted raster band numbering verification failed")
        if "checksum" not in band_meta:
            raise RuntimeError("persisted raster band checksum is missing")
        if _array_checksum(values) != band_meta["checksum"]:
            raise RuntimeError("persisted raster band checksum verification failed")
        if expected_bands is not None:
            expected_name, expected_values = expected_bands[index - 1]
            if band_meta["name"] != expected_name:
                raise RuntimeError("persisted raster band name verification failed")
            if not np.array_equal(values, expected_values, equal_nan=True):
                raise RuntimeError("persisted raster value verification failed")

    checksum = str(row["checksum"])
    computed = RasterCodec.content_checksum(definition, decoded_bands, manifest)
    if expected_checksum is not None and checksum != expected_checksum:
        raise RuntimeError("persisted raster checksum verification failed")
    if checksum != computed:
        raise RuntimeError("persisted raster checksum verification failed")

    product = await session.get(IntensityFieldProduct, product_id)
    if product is None:
        raise LookupError("intensity product not found")
    if product.output_checksum is not None and product.output_checksum != checksum:
        raise RuntimeError("persisted product checksum verification failed")

    return decoded_bands, {
        "crs": CRS.from_epsg(actual_srid).to_string() if actual_srid else None,
        "width": actual_width,
        "height": actual_height,
        "srid": actual_srid,
        "resolution_m": abs(scale_x),
        "origin_x": origin_x,
        "origin_y": origin_y,
        "grid_definition_version": definition.version,
        "bands": manifest_bands,
        "checksum": checksum,
    }


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
    if not values:
        raise RuntimeError("persisted raster has no bands")
    if expected_count is not None and len(values) != expected_count:
        raise RuntimeError("persisted raster band count verification failed")
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
