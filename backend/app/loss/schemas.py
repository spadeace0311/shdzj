from pydantic import BaseModel


class LossValueResponse(BaseModel):
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    value_type: str
    value_status: str
    numeric_value: float | None
    unit: str
    precision: int | None
    quality_grade: str
    note: str | None


class LossProductResponse(BaseModel):
    product_id: str
    product_type: str
    status: str
    quality_grade: str
    calibration_status: str
    coverage_ratio: float
    partial_scope: bool
    needs_review: bool
    spatialized_estimate: bool
    algorithm_version: str
    parameter_version: str
    region_profile_version: str
    output_checksum: str | None
    statistics: dict
    metrics: list[LossValueResponse]
    reason: str | None


class LossResultResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    effective_run_id: str | None
    is_fallback: bool
    products: list[LossProductResponse]


class LossAreaFeatureResponse(BaseModel):
    area_scope: str
    area_code: str
    area_name: str
    geometry: dict
    metrics: list[LossValueResponse]


class LossAreaResponse(BaseModel):
    run_id: str
    scope: str
    features: list[LossAreaFeatureResponse]


class LossGridBandResponse(BaseModel):
    name: str
    unit: str | None
    precision: int | None


class LossGridArtifactResponse(BaseModel):
    product_id: str
    checksum: str
    width: int
    height: int
    srid: int
    bbox: tuple[float, float, float, float]
    spatial_allocation_rule: str
    coverage_ratio: float
    spatialized_estimate: bool
    bands: list[LossGridBandResponse]
    tile_template: str
