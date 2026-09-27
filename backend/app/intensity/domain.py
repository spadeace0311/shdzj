from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import numpy as np


def _readonly_array(value: np.ndarray) -> np.ndarray:
    frozen = np.array(value, copy=True)
    frozen.setflags(write=False)
    return frozen


def _readonly_optional_array(value: np.ndarray | None) -> np.ndarray | None:
    if value is None:
        return None
    return _readonly_array(value)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_utc(value)


class ProductType(StrEnum):
    MODEL = "model"
    INSTRUMENT = "instrument"
    FUSION = "fusion"


class ProductStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    STALE = "stale"


class InstrumentQuality(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q0 = "Q0"


class FusionMode(StrEnum):
    FULL = "full"
    MODEL_ONLY = "model_only"


class FusionQuality(StrEnum):
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F0 = "F0"


class DirectionStatus(StrEnum):
    RESOLVED = "resolved"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class IntensityEventSnapshot:
    event_id: str
    revision_id: str
    magnitude: float
    longitude: float
    latitude: float
    report_ingested_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "report_ingested_at",
            _require_utc(self.report_ingested_at),
        )


@dataclass(frozen=True, slots=True)
class GridDefinition:
    version: str
    crs: str
    resolution_m: int
    origin_x: float
    origin_y: float
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.resolution_m <= 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("grid dimensions and resolution must be positive")
        if not self.version.strip() or not self.crs.strip():
            raise ValueError("grid version and crs must not be empty")

    @property
    def cell_count(self) -> int:
        return self.width * self.height

    def center_xy(self, row: int, column: int) -> tuple[float, float]:
        if not 0 <= row < self.height or not 0 <= column < self.width:
            raise IndexError("grid coordinate outside definition")
        return (
            self.origin_x + (column + 0.5) * self.resolution_m,
            self.origin_y - (row + 0.5) * self.resolution_m,
        )


@dataclass(frozen=True, slots=True)
class GridSamples:
    definition: GridDefinition
    rows: np.ndarray
    columns: np.ndarray
    x_km: np.ndarray
    y_km: np.ndarray
    longitude: np.ndarray
    latitude: np.ndarray
    distance_km: np.ndarray
    azimuth_deg: np.ndarray

    def __post_init__(self) -> None:
        for name in (
            "rows",
            "columns",
            "x_km",
            "y_km",
            "longitude",
            "latitude",
            "distance_km",
            "azimuth_deg",
        ):
            object.__setattr__(self, name, _readonly_array(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class DirectionDecision:
    status: DirectionStatus
    source: str | None
    strike_deg: float | None
    candidate_fault_id: str | None = None
    candidate_distance_km: float | None = None


@dataclass(frozen=True, slots=True)
class ModelField:
    values: np.ndarray
    sigma: np.ndarray
    extrapolated: bool
    direction: DirectionDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _readonly_array(self.values))
        object.__setattr__(self, "sigma", _readonly_array(self.sigma))


@dataclass(frozen=True, slots=True)
class InstrumentProduct:
    status: ProductStatus
    product_id: str | None
    product_version: str | None
    observed_at: datetime | None
    source: str
    grid_version: str | None
    values: np.ndarray | None
    sigma: np.ndarray | None
    quality_codes: np.ndarray | None
    coverage_ratio: float
    reason: str | None = None
    generated_at: datetime | None = None
    crs: str | None = None
    resolution_m: float | None = None
    raw_checksum: str | None = None
    normalized_checksum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _optional_utc(self.observed_at))
        object.__setattr__(self, "generated_at", _optional_utc(self.generated_at))
        object.__setattr__(
            self,
            "values",
            _readonly_optional_array(self.values),
        )
        object.__setattr__(
            self,
            "sigma",
            _readonly_optional_array(self.sigma),
        )
        object.__setattr__(
            self,
            "quality_codes",
            _readonly_optional_array(self.quality_codes),
        )


@dataclass(frozen=True, slots=True)
class FusionField:
    values: np.ndarray
    sigma: np.ndarray
    p10: np.ndarray
    p90: np.ndarray
    model_weight: np.ndarray
    instrument_weight: np.ndarray
    quality_codes: np.ndarray
    mode: FusionMode
    quality: FusionQuality
    coverage_ratio: float

    def __post_init__(self) -> None:
        for name in (
            "values",
            "sigma",
            "p10",
            "p90",
            "model_weight",
            "instrument_weight",
            "quality_codes",
        ):
            object.__setattr__(self, name, _readonly_array(getattr(self, name)))
