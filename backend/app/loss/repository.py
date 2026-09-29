from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import numpy as np
from pyproj import CRS
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition
from app.loss.artifacts import LossArtifactCodec
from app.loss.domain import (
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
)
from app.loss.models import LossMetricValue, LossProduct, LossProductRaster
from app.loss.validation import (
    GridResidual,
    LossValidationSnapshot,
    ValidationInputUnavailable,
)


LOSS_RASTER_CHECKSUM_NAMESPACE = "loss-raster-content-v1"
_VALIDATION_PRODUCT_TYPES = frozenset(
    {
        LossProductType.BUILDING_DAMAGE,
        LossProductType.POPULATION_IMPACT,
        LossProductType.CASUALTIES,
        LossProductType.ECONOMIC_LOSS,
        LossProductType.RESOURCE_DEMAND,
    }
)


@dataclass(frozen=True, slots=True)
class LossMetricValueWrite:
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    value_type: LossValueType
    value_status: LossMetricValueStatus
    numeric_value: float | None
    unit: str
    precision: int | None
    quality_grade: LossQualityGrade
    note: str | None


@dataclass(frozen=True, slots=True)
class LossRasterBandWrite:
    name: str
    values: np.ndarray
    unit: str
    precision: int | None


@dataclass(frozen=True, slots=True)
class LossRasterWrite:
    raster_version: str
    definition: GridDefinition
    bands: tuple[LossRasterBandWrite, ...]
    band_manifest: dict
    checksum: str
    spatial_allocation_rule: str
    coverage_ratio: float


@dataclass(frozen=True, slots=True)
class LossProductWrite:
    run_id: UUID
    task_id: UUID
    product_type: LossProductType
    status: LossProductStatus
    quality_grade: LossQualityGrade
    calibration_status: LossCalibrationStatus
    coverage_ratio: float
    partial_scope: bool
    needs_review: bool
    spatialized_estimate: bool
    algorithm_version: str
    parameter_version: str
    region_profile_version: str
    input_fingerprint: str
    input_checksum: str
    output_checksum: str
    statistics: dict
    metrics: tuple[LossMetricValueWrite, ...]
    raster: LossRasterWrite | None
    reason: str | None


class LossRepository:
    async def write_product(
        self,
        session: AsyncSession,
        write: LossProductWrite,
    ) -> LossProduct:
        existing = await session.scalar(
            select(LossProduct)
            .where(
                LossProduct.run_id == write.run_id,
                LossProduct.product_type == write.product_type.value,
            )
            .with_for_update()
        )
        if existing is not None:
            existing_raster_bands = (
                _normalized_loss_bands(write.raster)
                if write.raster is not None
                else None
            )
            await _validate_existing_product(
                session,
                write,
                existing,
                existing_raster_bands,
            )
            return existing

        normalized_bands = _validate_product_write(write)
        now = datetime.now(UTC)
        product = LossProduct(
            id=uuid4(),
            run_id=write.run_id,
            task_id=write.task_id,
            product_type=write.product_type.value,
            status=write.status.value,
            quality_grade=write.quality_grade.value,
            calibration_status=write.calibration_status.value,
            coverage_ratio=write.coverage_ratio,
            partial_scope=write.partial_scope,
            needs_review=write.needs_review,
            spatialized_estimate=write.spatialized_estimate,
            algorithm_version=write.algorithm_version,
            parameter_version=write.parameter_version,
            region_profile_version=write.region_profile_version,
            input_fingerprint=write.input_fingerprint,
            input_checksum=write.input_checksum,
            output_checksum=write.output_checksum,
            statistics=write.statistics,
            reason=write.reason,
            created_at=now,
            completed_at=now,
            published_at=None,
        )
        session.add(product)
        await session.flush()

        for metric in write.metrics:
            session.add(
                LossMetricValue(
                    product_id=product.id,
                    area_scope=metric.area_scope,
                    area_code=metric.area_code,
                    area_name=metric.area_name,
                    metric_key=metric.metric_key,
                    value_type=metric.value_type.value,
                    value_status=metric.value_status.value,
                    numeric_value=metric.numeric_value,
                    unit=metric.unit,
                    precision=metric.precision,
                    quality_grade=metric.quality_grade.value,
                    note=metric.note,
                )
            )

        if write.raster is not None:
            if normalized_bands is None:
                raise RuntimeError("validated raster bands are unavailable")
            computed_checksum = RasterCodec.content_checksum(
                write.raster.definition,
                normalized_bands,
                write.raster.band_manifest,
                checksum_namespace=LOSS_RASTER_CHECKSUM_NAMESPACE,
            )
            if computed_checksum != write.raster.checksum:
                raise ValueError(
                    "loss raster checksum does not match persisted content"
                )
            await _insert_raster(
                session,
                product_id=product.id,
                write=write.raster,
                normalized_bands=normalized_bands,
                created_at=now,
            )
            await _load_and_verify_raster(
                session,
                product.id,
                expected_definition=write.raster.definition,
                expected_bands=normalized_bands,
                expected_checksum=write.raster.checksum,
            )

        await session.flush()
        return product

    async def get_product(
        self,
        session: AsyncSession,
        run_id: UUID,
        product_type: LossProductType,
    ) -> LossProduct | None:
        return await session.scalar(
            select(LossProduct).where(
                LossProduct.run_id == run_id,
                LossProduct.product_type == product_type.value,
            )
        )

    async def list_products(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> list[LossProduct]:
        products = (
            await session.scalars(
                select(LossProduct)
                .where(LossProduct.run_id == run_id)
                .order_by(LossProduct.product_type)
            )
        ).all()
        return list(products)

    async def load_raster(
        self,
        session: AsyncSession,
        product_id: UUID,
    ) -> tuple[list[np.ndarray], dict]:
        product = await session.get(LossProduct, product_id)
        if product is None:
            raise LookupError("loss product not found")
        return await _load_and_verify_raster(session, product_id)

    async def load_validation_inputs(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> LossValidationSnapshot:
        products = (
            await session.scalars(
                select(LossProduct)
                .where(
                    LossProduct.run_id == run_id,
                    LossProduct.product_type.in_(
                        [item.value for item in _VALIDATION_PRODUCT_TYPES]
                    ),
                )
                .order_by(LossProduct.product_type)
            )
        ).all()
        by_type = {
            LossProductType(product.product_type): product
            for product in products
        }
        missing = sorted(
            item.value for item in _VALIDATION_PRODUCT_TYPES - set(by_type)
        )
        if missing:
            raise ValidationInputUnavailable(
                "loss validation inputs require exactly one current product "
                "for every model type"
            )
        for product in products:
            if product.status != LossProductStatus.COMPLETE.value:
                raise ValidationInputUnavailable(
                    f"loss product {product.product_type} is not complete"
                )

        decoded = {}
        fingerprints: dict[LossProductType, str | None] = {}
        for product_type, product in by_type.items():
            statistics = dict(product.statistics or {})
            payload = statistics.get("validation_payload")
            if payload is None:
                raise ValidationInputUnavailable(
                    f"loss product {product_type.value} has no validation payload"
                )
            decoded[product_type] = LossArtifactCodec.decode_validation_payload(
                product_type,
                payload,
            )
            fingerprint = statistics.get("data_asset_snapshot_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                raise ValidationInputUnavailable(
                    f"loss product {product_type.value} has no data-asset "
                    "snapshot fingerprint"
                )
            fingerprints[product_type] = fingerprint

        building_product = by_type[LossProductType.BUILDING_DAMAGE]
        coverage_ratio = float(building_product.coverage_ratio)
        if not math.isfinite(coverage_ratio) or not 0.0 <= coverage_ratio <= 1.0:
            raise ValidationInputUnavailable(
                "building damage coverage ratio must be finite and between "
                "zero and one"
            )
        grid_residuals = await _load_grid_residuals(
            session,
            building_product.id,
        )

        return LossValidationSnapshot(
            buildings=decoded[LossProductType.BUILDING_DAMAGE],
            population=decoded[LossProductType.POPULATION_IMPACT],
            casualties=decoded[LossProductType.CASUALTIES],
            economic=decoded[LossProductType.ECONOMIC_LOSS],
            resources=decoded[LossProductType.RESOURCE_DEMAND],
            coverage_ratio=coverage_ratio,
            grid_residuals=grid_residuals,
            product_snapshot_fingerprints=fingerprints,
        )


def _validate_product_write(
    write: LossProductWrite,
) -> list[tuple[str, np.ndarray]] | None:
    if not math.isfinite(float(write.coverage_ratio)) or not 0.0 <= float(
        write.coverage_ratio
    ) <= 1.0:
        raise ValueError("loss product coverage ratio must be between zero and one")
    for metric in write.metrics:
        _validate_metric_write(metric)
    try:
        json.dumps(write.statistics, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("loss product statistics must be JSON compatible") from exc
    if write.raster is None:
        return None
    return _normalized_loss_bands(write.raster)


def _validate_metric_write(metric: LossMetricValueWrite) -> None:
    if metric.numeric_value is None:
        if metric.value_status not in {
            LossMetricValueStatus.UNAVAILABLE,
            LossMetricValueStatus.NOT_APPLICABLE,
        }:
            raise ValueError(
                "null metric requires unavailable or not_applicable status"
            )
        return
    numeric_value = float(metric.numeric_value)
    if not math.isfinite(numeric_value) or numeric_value < 0.0:
        raise ValueError("loss metric numeric value must be finite and non-negative")
    if metric.value_status == LossMetricValueStatus.ZERO and numeric_value != 0:
        raise ValueError("zero status requires a zero value")
    if metric.value_status in {
        LossMetricValueStatus.UNAVAILABLE,
        LossMetricValueStatus.NOT_APPLICABLE,
    }:
        raise ValueError("unavailable metric must not carry a numeric value")


def _normalized_loss_bands(
    raster: LossRasterWrite,
) -> list[tuple[str, np.ndarray]]:
    if not raster.bands:
        raise ValueError("loss raster requires at least one band")
    expected = (raster.definition.height, raster.definition.width)
    normalized: list[tuple[str, np.ndarray]] = []
    for band in raster.bands:
        if not isinstance(band.name, str) or not band.name.strip():
            raise ValueError("raster band names must not be empty")
        values = np.asarray(band.values, dtype=np.float64)
        if values.shape != expected:
            raise ValueError("raster band shape does not match grid definition")
        if not np.all(np.isfinite(values)):
            raise ValueError("raster band values must be finite")
        normalized.append((band.name, np.ascontiguousarray(values)))
    if len({name for name, _ in normalized}) != len(normalized):
        raise ValueError("raster band names must be unique")

    manifest_bands = raster.band_manifest.get("bands")
    if not isinstance(manifest_bands, list) or len(manifest_bands) != len(
        normalized
    ):
        raise ValueError("raster band manifest does not match raster bands")
    for index, (band, (name, _)) in enumerate(
        zip(manifest_bands, normalized, strict=True),
        start=1,
    ):
        if not isinstance(band, dict):
            raise ValueError("raster band manifest entries must be mappings")
        if int(band.get("number", -1)) != index:
            raise ValueError("raster band manifest numbering is invalid")
        if str(band.get("name", "")) != name:
            raise ValueError("raster band manifest name does not match raster band")
    return normalized


async def _validate_existing_product(
    session: AsyncSession,
    write: LossProductWrite,
    existing: LossProduct,
    normalized_bands: list[tuple[str, np.ndarray]] | None,
) -> None:
    incoming = {
        "run_id": write.run_id,
        "task_id": write.task_id,
        "product_type": write.product_type.value,
        "status": write.status.value,
        "quality_grade": write.quality_grade.value,
        "calibration_status": write.calibration_status.value,
        "coverage_ratio": float(write.coverage_ratio),
        "partial_scope": write.partial_scope,
        "needs_review": write.needs_review,
        "spatialized_estimate": write.spatialized_estimate,
        "algorithm_version": write.algorithm_version,
        "parameter_version": write.parameter_version,
        "region_profile_version": write.region_profile_version,
        "input_fingerprint": write.input_fingerprint,
        "input_checksum": write.input_checksum,
        "output_checksum": write.output_checksum,
        "statistics": write.statistics,
        "reason": write.reason,
    }
    existing_values = {
        "run_id": existing.run_id,
        "task_id": existing.task_id,
        "product_type": existing.product_type,
        "status": existing.status,
        "quality_grade": existing.quality_grade,
        "calibration_status": existing.calibration_status,
        "coverage_ratio": float(existing.coverage_ratio),
        "partial_scope": existing.partial_scope,
        "needs_review": existing.needs_review,
        "spatialized_estimate": existing.spatialized_estimate,
        "algorithm_version": existing.algorithm_version,
        "parameter_version": existing.parameter_version,
        "region_profile_version": existing.region_profile_version,
        "input_fingerprint": existing.input_fingerprint,
        "input_checksum": existing.input_checksum,
        "output_checksum": existing.output_checksum,
        "statistics": dict(existing.statistics or {}),
        "reason": existing.reason,
    }
    for field_name in incoming:
        if not _same_value(incoming[field_name], existing_values[field_name]):
            raise ValueError("published loss product cannot be overwritten")
    await _validate_existing_metrics(session, existing.id, write.metrics)
    await _validate_existing_raster(
        session,
        existing.id,
        write.raster,
        normalized_bands,
    )


async def _validate_existing_metrics(
    session: AsyncSession,
    product_id: UUID,
    incoming_metrics: tuple[LossMetricValueWrite, ...],
) -> None:
    existing_metrics = (
        await session.scalars(
            select(LossMetricValue)
            .where(LossMetricValue.product_id == product_id)
            .order_by(
                LossMetricValue.area_scope,
                LossMetricValue.area_code,
                LossMetricValue.metric_key,
                LossMetricValue.value_type,
            )
        )
    ).all()
    incoming = sorted(
        incoming_metrics,
        key=lambda metric: (
            metric.area_scope,
            metric.area_code,
            metric.metric_key,
            metric.value_type.value,
        ),
    )
    if len(existing_metrics) != len(incoming):
        raise ValueError("published loss product cannot be overwritten")
    for metric, row in zip(incoming, existing_metrics, strict=True):
        if (
            metric.area_scope != row.area_scope
            or metric.area_code != row.area_code
            or metric.area_name != row.area_name
            or metric.metric_key != row.metric_key
            or metric.value_type.value != row.value_type
            or metric.value_status.value != row.value_status
            or not _same_value(metric.numeric_value, row.numeric_value)
            or metric.unit != row.unit
            or metric.precision != row.precision
            or metric.quality_grade.value != row.quality_grade
            or metric.note != row.note
        ):
            raise ValueError("published loss product cannot be overwritten")


async def _validate_existing_raster(
    session: AsyncSession,
    product_id: UUID,
    incoming: LossRasterWrite | None,
    normalized_bands: list[tuple[str, np.ndarray]] | None,
) -> None:
    rows = (
        await session.scalars(
            select(LossProductRaster).where(
                LossProductRaster.product_id == product_id
            )
        )
    ).all()
    if incoming is None:
        if rows:
            raise ValueError("published loss product cannot be overwritten")
        return
    if len(rows) != 1 or normalized_bands is None:
        raise ValueError("published loss product cannot be overwritten")
    row = rows[0]
    srid = CRS.from_user_input(incoming.definition.crs).to_epsg()
    if (
        row.raster_version != incoming.raster_version
        or row.checksum != incoming.checksum
        or row.width != incoming.definition.width
        or row.height != incoming.definition.height
        or row.srid != srid
        or row.spatial_allocation_rule != incoming.spatial_allocation_rule
        or not _same_value(float(row.coverage_ratio), incoming.coverage_ratio)
        or dict(row.band_manifest or {}) != incoming.band_manifest
    ):
        raise ValueError("published loss product cannot be overwritten")
    computed = RasterCodec.content_checksum(
        incoming.definition,
        normalized_bands,
        incoming.band_manifest,
        checksum_namespace=LOSS_RASTER_CHECKSUM_NAMESPACE,
    )
    if computed != row.checksum:
        raise ValueError("published loss product cannot be overwritten")


async def _insert_raster(
    session: AsyncSession,
    *,
    product_id: UUID,
    write: LossRasterWrite,
    normalized_bands: list[tuple[str, np.ndarray]],
    created_at: datetime,
) -> None:
    srid = CRS.from_user_input(write.definition.crs).to_epsg()
    if srid is None:
        raise ValueError("grid CRS must map to an EPSG code")
    raster_expression = _loss_raster_expression(len(normalized_bands))
    bindings = {
        "id": uuid4(),
        "product_id": product_id,
        "raster_version": write.raster_version,
        "manifest": json.dumps(
            write.band_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "checksum": write.checksum,
        "width": write.definition.width,
        "height": write.definition.height,
        "upperleftx": write.definition.origin_x,
        "upperlefty": write.definition.origin_y,
        "scalex": float(write.definition.resolution_m),
        "scaley": -float(write.definition.resolution_m),
        "srid": srid,
        "spatial_allocation_rule": write.spatial_allocation_rule,
        "coverage_ratio": write.coverage_ratio,
        "created_at": created_at,
    }
    for index, (_, values) in enumerate(normalized_bands, start=1):
        bindings[f"values_{index}"] = np.asarray(
            values,
            dtype=np.float64,
        ).tolist()

    await session.execute(
        text(
            f"""
            INSERT INTO loss_product_rasters (
                id,
                product_id,
                raster_version,
                rast,
                band_manifest,
                checksum,
                width,
                height,
                srid,
                spatial_allocation_rule,
                coverage_ratio,
                created_at
            )
            VALUES (
                :id,
                :product_id,
                :raster_version,
                {raster_expression},
                CAST(:manifest AS jsonb),
                :checksum,
                :width,
                :height,
                :srid,
                :spatial_allocation_rule,
                :coverage_ratio,
                :created_at
            )
            """
        ),
        bindings,
    )


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
                    raster_version,
                    width AS stored_width,
                    height AS stored_height,
                    srid AS stored_srid,
                    spatial_allocation_rule,
                    coverage_ratio
                FROM loss_product_rasters
                WHERE product_id = :product_id
                """
            ),
            {"product_id": product_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("loss raster not found")

    manifest = row["band_manifest"]
    if not isinstance(manifest, dict):
        raise RuntimeError("persisted loss raster manifest is not a mapping")
    manifest = dict(manifest)
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

    if expected_definition is None:
        definition = _grid_definition_from_raster_row(manifest, row)
    else:
        definition = expected_definition

    if actual_width != definition.width or actual_height != definition.height:
        raise RuntimeError("persisted loss raster dimensions verification failed")
    if stored_width != actual_width or stored_height != actual_height:
        raise RuntimeError("persisted loss raster dimension metadata verification failed")
    expected_srid = _expected_grid_srid(definition)
    if actual_srid != expected_srid or stored_srid != actual_srid:
        raise RuntimeError("persisted loss raster SRID verification failed")
    _check_float_equal(scale_x, float(definition.resolution_m), "scale x")
    _check_float_equal(scale_y, -float(definition.resolution_m), "scale y")
    _check_float_equal(origin_x, float(definition.origin_x), "origin x")
    _check_float_equal(origin_y, float(definition.origin_y), "origin y")

    manifest_bands = _manifest_bands(manifest)
    decoded_bands = await _dump_loss_band_values(session, product_id)
    if len(decoded_bands) != len(manifest_bands):
        raise RuntimeError("persisted loss raster band count verification failed")
    for index, (band_meta, values) in enumerate(
        zip(manifest_bands, decoded_bands, strict=True),
        start=1,
    ):
        if int(band_meta["number"]) != index:
            raise RuntimeError("persisted loss raster band numbering verification failed")
        if expected_bands is not None:
            expected_name, expected_values = expected_bands[index - 1]
            if band_meta["name"] != expected_name:
                raise RuntimeError("persisted loss raster band name verification failed")
            if not np.array_equal(values, expected_values, equal_nan=True):
                raise RuntimeError("persisted loss raster value verification failed")

    checksum = str(row["checksum"])
    if expected_checksum is not None and checksum != expected_checksum:
        raise RuntimeError("persisted loss raster checksum verification failed")
    if expected_definition is not None or isinstance(manifest.get("grid"), dict):
        computed = RasterCodec.content_checksum(
            definition,
            decoded_bands,
            manifest,
            checksum_namespace=LOSS_RASTER_CHECKSUM_NAMESPACE,
        )
        if checksum != computed:
            raise RuntimeError("persisted loss raster checksum verification failed")

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


async def _dump_loss_band_values(
    session: AsyncSession,
    product_id: UUID,
) -> list[np.ndarray]:
    values = (
        await session.scalars(
            text(
                """
                SELECT ST_DumpValues(rast, band_number, false)
                FROM loss_product_rasters
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
        raise RuntimeError("persisted loss raster has no bands")
    return [np.asarray(value, dtype=np.float64) for value in values]


async def _load_grid_residuals(
    session: AsyncSession,
    building_product_id: UUID,
) -> tuple[GridResidual, ...]:
    metric_codes = {
        str(code)
        for code in (
            await session.scalars(
                text(
                    """
                    SELECT DISTINCT area_code
                    FROM loss_metric_values
                    WHERE product_id = :product_id
                      AND area_scope = 'town'
                    """
                ),
                {"product_id": building_product_id},
            )
        ).all()
    }
    raster_row = (
        await session.execute(
            text(
                """
                SELECT band_manifest
                FROM loss_product_rasters
                WHERE product_id = :product_id
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {"product_id": building_product_id},
        )
    ).mappings().one_or_none()
    if raster_row is None:
        raise ValidationInputUnavailable(
            "building damage product has no persisted raster manifest"
        )
    manifest = raster_row["band_manifest"]
    if not isinstance(manifest, dict):
        raise ValidationInputUnavailable(
            "building damage raster manifest is not a mapping"
        )
    residuals = _residuals_from_manifest(manifest)
    residual_town_codes = {
        residual.area_code
        for residual in residuals
        if residual.area_scope == "town"
    }
    if residual_town_codes != metric_codes:
        raise ValidationInputUnavailable(
            "building damage grid residual towns do not match persisted "
            "town metrics"
        )
    return tuple(sorted(residuals, key=lambda item: (item.area_scope, item.area_code)))


def _residuals_from_manifest(manifest: dict) -> list[GridResidual]:
    reconciliation = manifest.get("reconciliation", {})
    if reconciliation is None:
        return []
    if not isinstance(reconciliation, dict):
        raise ValidationInputUnavailable(
            "building damage raster reconciliation must be a mapping"
        )
    residuals: list[GridResidual] = []
    for area_scope, entries in reconciliation.items():
        if not isinstance(entries, dict):
            raise ValidationInputUnavailable(
                "building damage raster reconciliation scope must be a mapping"
            )
        for area_code, entry in entries.items():
            if isinstance(entry, dict):
                if "residual" not in entry:
                    raise ValidationInputUnavailable(
                        "building damage raster reconciliation entry has no residual"
                    )
                raw_residual = entry["residual"]
            else:
                raw_residual = entry
            try:
                residual = float(raw_residual)
            except (TypeError, ValueError) as exc:
                raise ValidationInputUnavailable(
                    "building damage raster residual must be numeric"
                ) from exc
            if not math.isfinite(residual):
                raise ValidationInputUnavailable(
                    "building damage raster residual must be finite"
                )
            residuals.append(
                GridResidual(
                    area_scope=str(area_scope),
                    area_code=str(area_code),
                    residual=residual,
                )
            )
    return residuals


def _same_value(incoming: object, existing: object) -> bool:
    if isinstance(incoming, float) and isinstance(existing, (int, float)):
        return math.isclose(float(incoming), float(existing), rel_tol=0.0, abs_tol=1e-12)
    if isinstance(existing, float) and isinstance(incoming, (int, float)):
        return math.isclose(float(incoming), float(existing), rel_tol=0.0, abs_tol=1e-12)
    return incoming == existing


def _grid_definition_from_raster_row(
    manifest: dict,
    row,
) -> GridDefinition:
    grid = manifest.get("grid")
    if isinstance(grid, dict):
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
            raise RuntimeError("persisted loss raster grid metadata is invalid") from exc
    return GridDefinition(
        version=str(row["raster_version"]),
        crs=CRS.from_epsg(int(row["actual_srid"])).to_string(),
        resolution_m=int(abs(float(row["scale_x"]))),
        origin_x=float(row["origin_x"]),
        origin_y=float(row["origin_y"]),
        width=int(row["actual_width"]),
        height=int(row["actual_height"]),
    )


def _manifest_bands(manifest: dict) -> list[dict]:
    bands = manifest.get("bands")
    if not isinstance(bands, list) or not bands:
        raise RuntimeError("persisted loss raster manifest has no bands")
    for band in bands:
        if not isinstance(band, dict) or {"number", "name"} - set(band):
            raise RuntimeError("persisted loss raster band manifest is invalid")
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
        raise RuntimeError(f"persisted loss raster {field_name} verification failed")


def _loss_raster_expression(band_count: int) -> str:
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
