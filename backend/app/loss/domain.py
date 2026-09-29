from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class LossModelType(StrEnum):
    BUILDING_DAMAGE = "building_damage"
    POPULATION_IMPACT = "population_impact"
    CASUALTIES = "casualties"
    ECONOMIC_LOSS = "economic_loss"
    RESOURCE_DEMAND = "resource_demand"


class LossProductType(StrEnum):
    BUILDING_DAMAGE = "building_damage"
    POPULATION_IMPACT = "population_impact"
    CASUALTIES = "casualties"
    ECONOMIC_LOSS = "economic_loss"
    RESOURCE_DEMAND = "resource_demand"
    VALIDATION = "validation"


class LossProductStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class LossQualityGrade(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L0 = "L0"


class LossCalibrationStatus(StrEnum):
    CALIBRATED = "calibrated"
    REFERENCE_UNCALIBRATED = "reference_uncalibrated"
    UNCALIBRATED = "uncalibrated"


class LossValueType(StrEnum):
    LOW = "low"
    CENTRAL = "central"
    HIGH = "high"


class LossMetricValueStatus(StrEnum):
    AVAILABLE = "available"
    ZERO = "zero"
    ROUNDED_TO_ZERO = "rounded_to_zero"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class DamageState(StrEnum):
    BASIC = "basic"
    SLIGHTLY_DAMAGED = "slightly_damaged"
    MODERATELY_DAMAGED = "moderately_damaged"
    SEVERELY_DAMAGED = "severely_damaged"
    COLLAPSED = "collapsed"


class BuildingStructure(StrEnum):
    HIGH_RISE = "high_rise"
    RC_FRAME = "rc_frame"
    MASONRY = "masonry"
    SINGLE_STOREY = "single_storey"
    OTHER = "other"


class ResourceKind(StrEnum):
    RESCUE_TEAM = "rescue_team"
    MEDICAL_TEAM = "medical_team"
    EPIDEMIC_TEAM = "epidemic_team"
    TENT = "tent"
    DRINKING_WATER = "drinking_water"
    TOILET = "toilet"
    CLOTHING = "clothing"
    QUILT = "quilt"
    FOOD = "food"
    BLANKET = "blanket"
    STRETCHER = "stretcher"
    SICKBED = "sickbed"


@dataclass(frozen=True, slots=True)
class LossRunContext:
    run_id: str
    event_id: str
    revision_id: str
    report_ingested_at: datetime
    region_id: str
    region_profile_version: str
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    fused_intensity_product_id: str
    fused_intensity_checksum: str
    data_asset_snapshot_checksum: str


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    model_id: str
    model_type: LossModelType
    formula_version: str
    applicable_region: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    source_citations: tuple[str, ...]
    source_requirements: tuple[str, ...]
    calibration_status: LossCalibrationStatus
    is_default: bool


@dataclass(frozen=True, slots=True)
class ScenarioParameters:
    values: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ParameterSetModel:
    model_id: str
    formula_version: str
    applicable_region: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    source_citations: tuple[str, ...]
    source_requirements: tuple[str, ...]
    scenarios: Mapping[LossValueType, ScenarioParameters]


@dataclass(frozen=True, slots=True)
class ParameterSet:
    version: str
    calibration_status: LossCalibrationStatus
    provenance: Mapping[str, object]
    models: Mapping[LossModelType, ParameterSetModel]
    checksum: str
    test_only: bool


@dataclass(frozen=True, slots=True)
class LossValue:
    value_type: LossValueType
    value_status: LossMetricValueStatus
    numeric_value: float | None
    quality_grade: LossQualityGrade
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LossMetric:
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    unit: str
    precision: int | None
    values: tuple[LossValue, ...]


@dataclass(frozen=True, slots=True)
class LossProductResult:
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
    statistics: Mapping[str, object]
    metrics: tuple[LossMetric, ...]
    raster_checksum: str | None = None
    reason: str | None = None
