import json
from collections import defaultdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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
from app.raster_tiles import MAX_TILE_ZOOM, MIN_TILE_ZOOM, render_raster_tile
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
    if not MIN_TILE_ZOOM <= z <= MAX_TILE_ZOOM:
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
                png = render_raster_tile(
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
