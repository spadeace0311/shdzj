import json
import math
from collections import defaultdict
from typing import Literal
from uuid import UUID

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.auth.router import require_role
from app.data_assets.models import DataAssetRecord
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.db import SessionFactory
from app.loss.domain import LossProductType
from app.loss.models import LossMetricValue, LossProduct, LossProductRaster
from app.loss.repository import LossRepository
from app.loss.schemas import (
    LossAreaFeatureResponse,
    LossAreaResponse,
    LossGridArtifactResponse,
    LossGridBandResponse,
    LossProductResponse,
    LossResultResponse,
    LossValueResponse,
)

router = APIRouter(prefix="/api/v1/assessments/runs", tags=["loss"])

_ASSESSMENT_READ_ROLES = (
    "superadmin",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)

_ADMIN_ASSET_KEYS = {
    "city": "shanghai.admin.city",
    "county": "shanghai.admin.county",
    "town": "shanghai.admin.town",
}

_MIN_ZOOM = 0
_MAX_ZOOM = 22
_TILE_SIZE = 256


def get_assessment_repository() -> AssessmentRepository:
    return AssessmentRepository()


@router.get(
    "/{run_id}/loss",
    response_model=LossResultResponse,
)
async def get_loss_result(
    run_id: UUID,
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> LossResultResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                effective = await repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                return await _build_loss_result(
                    session,
                    run,
                    effective,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


@router.get(
    "/{run_id}/loss/products/{product_type}",
    response_model=LossProductResponse,
)
async def get_loss_product(
    run_id: UUID,
    product_type: LossProductType,
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> LossProductResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                effective = await repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                result_run = effective or run
                product = await LossRepository().get_product(
                    session,
                    result_run.id,
                    product_type,
                )
                if product is None:
                    raise LookupError("loss_product_not_found")
                metrics = await _metric_rows(session, [product.id])
                return _product_response(
                    product,
                    metrics.get(product.id, []),
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


@router.get(
    "/{run_id}/loss/areas",
    response_model=LossAreaResponse,
)
async def get_loss_areas(
    run_id: UUID,
    scope: Literal["city", "county", "town"] = Query(...),
    repository: AssessmentRepository = Depends(get_assessment_repository),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> LossAreaResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                run = await repository.get_run(session, run_id)
                if run is None:
                    raise LookupError("assessment_run_not_found")
                effective = await repository.get_effective_run(
                    session,
                    event_id=str(run.event_id),
                )
                result_run = effective or run
                rows = (
                    await session.scalars(
                        select(LossMetricValue)
                        .join(
                            LossProduct,
                            LossMetricValue.product_id == LossProduct.id,
                        )
                        .where(
                            LossProduct.run_id == result_run.id,
                            LossMetricValue.area_scope == scope,
                        )
                        .order_by(
                            LossMetricValue.area_code,
                            LossMetricValue.metric_key,
                            LossMetricValue.value_type,
                        )
                    )
                ).all()
                grouped: dict[tuple[str, str], list[LossMetricValue]] = defaultdict(
                    list
                )
                for row in rows:
                    grouped[(row.area_code, row.area_name or row.area_code)].append(
                        row
                    )
                geometries = await _area_geometries(
                    session,
                    result_run.id,
                    scope,
                    [code for code, _ in grouped],
                )
                features = []
                for code, name in grouped:
                    geometry = geometries.get(code)
                    if geometry is not None:
                        features.append(
                            _area_feature(
                                scope,
                                code,
                                name,
                                grouped[(code, name)],
                                geometry,
                            )
                        )
                return LossAreaResponse(
                    run_id=str(result_run.id),
                    scope=scope,
                    features=features,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


@router.get(
    "/{run_id}/loss/artifact",
    response_model=LossGridArtifactResponse,
)
async def get_loss_artifact(
    run_id: UUID,
    product_id: UUID,
    band: str | None = Query(default=None),
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> LossGridArtifactResponse:
    try:
        async with SessionFactory() as session:
            async with session.begin():
                product = await session.scalar(
                    select(LossProduct).where(
                        LossProduct.id == product_id,
                        LossProduct.run_id == run_id,
                    )
                )
                if product is None:
                    raise LookupError("loss_product_not_found")
                raster = await session.scalar(
                    select(LossProductRaster).where(
                        LossProductRaster.product_id == product.id
                    )
                )
                if raster is None:
                    raise LookupError("loss_raster_not_found")
                return _artifact_response(
                    run_id,
                    product,
                    raster,
                    band,
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


@router.get(
    "/{run_id}/loss/artifact/{product_id}/{band}/{z}/{x}/{y}.png",
    response_class=Response,
)
async def get_loss_tile(
    run_id: UUID,
    product_id: UUID,
    band: str,
    z: int,
    x: int,
    y: int,
    _current_user: object = Depends(require_role(*_ASSESSMENT_READ_ROLES)),
) -> Response:
    if not _MIN_ZOOM <= z <= _MAX_ZOOM:
        raise HTTPException(
            status_code=422,
            detail="tile zoom must be between 0 and 22",
        )
    try:
        async with SessionFactory() as session:
            async with session.begin():
                product = await session.scalar(
                    select(LossProduct).where(
                        LossProduct.id == product_id,
                        LossProduct.run_id == run_id,
                    )
                )
                if product is None:
                    raise LookupError("loss_product_not_found")
                raster = await session.scalar(
                    select(LossProductRaster).where(
                        LossProductRaster.product_id == product.id
                    )
                )
                if raster is None:
                    raise LookupError("loss_raster_not_found")
                values, metadata = await LossRepository().load_raster(
                    session,
                    product.id,
                )
                band_index = _band_index(metadata["bands"], band)
                if band_index is None:
                    raise LookupError("loss_raster_band_not_found")
                png = _render_tile(
                    values[band_index],
                    metadata,
                    z,
                    x,
                    y,
                )
                return Response(content=png, media_type="image/png")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="assessment storage is unavailable",
        ) from exc


async def build_assessment_loss(
    session,
    run: AssessmentRun,
    effective: AssessmentRun | None,
) -> LossResultResponse | None:
    result_run = effective or run
    products = await LossRepository().list_products(session, result_run.id)
    if not products:
        return None
    return await _build_loss_result(session, run, effective)


async def _build_loss_result(
    session,
    requested_run: AssessmentRun,
    effective: AssessmentRun | None,
) -> LossResultResponse:
    result_run = effective or requested_run
    products = await LossRepository().list_products(session, result_run.id)
    metric_map = await _metric_rows(
        session,
        [product.id for product in products],
    )
    return LossResultResponse(
        run_id=str(result_run.id),
        event_id=str(result_run.event_id),
        revision_id=str(result_run.revision_id),
        effective_run_id=str(effective.id) if effective is not None else None,
        is_fallback=effective is not None and effective.id != requested_run.id,
        products=[
            _product_response(product, metric_map.get(product.id, []))
            for product in products
        ],
    )


async def _metric_rows(
    session,
    product_ids: list[UUID],
) -> dict[UUID, list[LossMetricValue]]:
    if not product_ids:
        return {}
    rows = (
        await session.scalars(
            select(LossMetricValue)
            .where(LossMetricValue.product_id.in_(product_ids))
            .order_by(
                LossMetricValue.product_id,
                LossMetricValue.area_scope,
                LossMetricValue.area_code,
                LossMetricValue.metric_key,
                LossMetricValue.value_type,
            )
        )
    ).all()
    grouped: dict[UUID, list[LossMetricValue]] = defaultdict(list)
    for row in rows:
        grouped[row.product_id].append(row)
    return grouped


def _product_response(
    product: LossProduct,
    metrics: list[LossMetricValue],
) -> LossProductResponse:
    return LossProductResponse(
        product_id=str(product.id),
        product_type=product.product_type,
        status=product.status,
        quality_grade=product.quality_grade,
        calibration_status=product.calibration_status,
        coverage_ratio=float(product.coverage_ratio),
        partial_scope=product.partial_scope,
        needs_review=product.needs_review,
        spatialized_estimate=product.spatialized_estimate,
        algorithm_version=product.algorithm_version,
        parameter_version=product.parameter_version,
        region_profile_version=product.region_profile_version,
        output_checksum=product.output_checksum,
        statistics=dict(product.statistics or {}),
        metrics=[_value_response(metric) for metric in metrics],
        reason=product.reason,
    )


def _value_response(metric: LossMetricValue) -> LossValueResponse:
    return LossValueResponse(
        area_scope=metric.area_scope,
        area_code=metric.area_code,
        area_name=metric.area_name,
        metric_key=metric.metric_key,
        value_type=metric.value_type,
        value_status=metric.value_status,
        numeric_value=(
            float(metric.numeric_value)
            if metric.numeric_value is not None
            else None
        ),
        unit=metric.unit,
        precision=metric.precision,
        quality_grade=metric.quality_grade,
        note=metric.note,
    )


async def _area_geometries(
    session,
    run_id: UUID,
    scope: str,
    area_codes: list[str],
) -> dict[str, dict]:
    if not area_codes:
        return {}
    locked_version = await DataAssetSnapshotService().get_locked_version(
        session,
        run_id=run_id,
        asset_key=_ADMIN_ASSET_KEYS[scope],
    )
    if locked_version is None:
        return {}
    rows = (
        await session.execute(
            select(
                DataAssetRecord.business_key,
                func.ST_AsGeoJSON(DataAssetRecord.geom).label("geometry"),
            )
            .where(
                DataAssetRecord.version_id == locked_version.id,
                DataAssetRecord.business_key.in_(area_codes),
            )
        )
    ).all()
    return {str(row.business_key): json.loads(str(row.geometry)) for row in rows}


def _area_feature(
    scope: str,
    code: str,
    name: str,
    metrics: list[LossMetricValue],
    geometry: dict,
) -> LossAreaFeatureResponse:
    return LossAreaFeatureResponse(
        area_scope=scope,
        area_code=code,
        area_name=name,
        geometry=geometry,
        metrics=[_value_response(metric) for metric in metrics],
    )


def _artifact_response(
    run_id: UUID,
    product: LossProduct,
    raster: LossProductRaster,
    selected_band: str | None,
) -> LossGridArtifactResponse:
    manifest = dict(raster.band_manifest or {})
    bands = manifest.get("bands", [])
    if not isinstance(bands, list) or not bands:
        raise LookupError("loss_raster_band_not_found")
    band_entries = [dict(item) for item in bands if isinstance(item, dict)]
    band_names = [str(item["name"]) for item in band_entries if "name" in item]
    if selected_band is None:
        if not band_names:
            raise LookupError("loss_raster_band_not_found")
        selected_band = band_names[0]
    if selected_band not in band_names:
        raise LookupError("loss_raster_band_not_found")

    grid = manifest.get("grid")
    if not isinstance(grid, dict):
        raise LookupError("loss_raster_not_found")
    origin_x = float(grid["origin_x"])
    origin_y = float(grid["origin_y"])
    resolution_m = float(grid["resolution_m"])
    width = int(grid["width"])
    height = int(grid["height"])
    bbox = (
        origin_x,
        origin_y - height * resolution_m,
        origin_x + width * resolution_m,
        origin_y,
    )
    tile_template = (
        f"/api/v1/assessments/runs/{run_id}/loss/artifact/"
        f"{product.id}/{selected_band}/{{z}}/{{x}}/{{y}}.png"
    )
    return LossGridArtifactResponse(
        product_id=str(product.id),
        checksum=raster.checksum,
        width=raster.width,
        height=raster.height,
        srid=raster.srid,
        bbox=bbox,
        spatial_allocation_rule=raster.spatial_allocation_rule,
        coverage_ratio=float(raster.coverage_ratio),
        spatialized_estimate=product.spatialized_estimate,
        bands=[
            LossGridBandResponse(
                name=str(item["name"]),
                unit=item.get("unit"),
                precision=item.get("precision"),
            )
            for item in band_entries
            if "name" in item
        ],
        tile_template=tile_template,
    )


def _band_index(bands: list[dict], band: str) -> int | None:
    for index, item in enumerate(bands):
        if str(item.get("name")) == band:
            return index
    return None


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    )
    south = math.degrees(
        math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n)))
    )
    west_merc, south_merc, east_merc, north_merc = transform_bounds(
        "EPSG:4326",
        "EPSG:3857",
        west,
        south,
        east,
        north,
    )
    return west_merc, south_merc, east_merc, north_merc


def _render_tile(
    values: np.ndarray,
    metadata: dict,
    z: int,
    x: int,
    y: int,
) -> bytes:
    source = np.ascontiguousarray(values, dtype=np.float64)
    src_transform = from_origin(
        float(metadata["origin_x"]),
        float(metadata["origin_y"]),
        float(metadata["resolution_m"]),
        float(metadata["resolution_m"]),
    )
    west, south, east, north = _tile_bounds(z, x, y)
    dst_transform = from_bounds(
        west,
        south,
        east,
        north,
        _TILE_SIZE,
        _TILE_SIZE,
    )
    destination = np.empty(
        (1, _TILE_SIZE, _TILE_SIZE),
        dtype=np.float64,
    )
    reproject(
        source=np.expand_dims(source, axis=0),
        destination=destination,
        src_transform=src_transform,
        src_crs=str(metadata["crs"]),
        src_nodata=np.nan,
        dst_transform=dst_transform,
        dst_crs="EPSG:3857",
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    tile_values = destination[0]
    finite = np.isfinite(tile_values)
    rgb = np.zeros((_TILE_SIZE, _TILE_SIZE, 3), dtype=np.uint8)
    if np.any(finite):
        present = tile_values[finite]
        value_min = float(np.min(present))
        value_max = float(np.max(present))
        if value_max > value_min:
            normalized = (tile_values[finite] - value_min) / (
                value_max - value_min
            )
        else:
            normalized = np.full(present.shape, 0.5, dtype=np.float64)
        rgb[finite] = _ramp_colors(normalized)
    with MemoryFile() as memory:
        with memory.open(
            driver="PNG",
            width=_TILE_SIZE,
            height=_TILE_SIZE,
            count=3,
            dtype="uint8",
        ) as dataset:
            dataset.write(rgb[:, :, 0], 1)
            dataset.write(rgb[:, :, 1], 2)
            dataset.write(rgb[:, :, 2], 3)
        return memory.read()


def _ramp_colors(normalized: np.ndarray) -> np.ndarray:
    stops = (
        (0.0, (13, 8, 135)),
        (0.25, (0, 120, 200)),
        (0.5, (60, 180, 75)),
        (0.75, (255, 220, 25)),
        (1.0, (215, 25, 28)),
    )
    colors = np.zeros((normalized.shape[0], 3), dtype=np.float64)
    for index in range(len(stops) - 1):
        start, start_color = stops[index]
        end, end_color = stops[index + 1]
        mask = (normalized >= start) & (normalized <= end)
        span = max(end - start, 1e-12)
        amount = (normalized[mask] - start) / span
        for channel in range(3):
            colors[mask, channel] = (
                start_color[channel]
                + amount * (end_color[channel] - start_color[channel])
            )
    return np.clip(colors, 0, 255).astype(np.uint8)
