from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from app.intensity.domain import InstrumentQuality


@dataclass(frozen=True, slots=True)
class AxisParameters:
    intercept: float
    magnitude_coefficient: float
    decay_coefficient: float
    distance_offset_km: float
    sigma: float


@dataclass(frozen=True, slots=True)
class ModelParameters:
    reference_doi: str
    long_axis: AxisParameters
    short_axis: AxisParameters
    valid_magnitude_min: float
    valid_magnitude_max: float
    solver_intensity_min: float
    solver_intensity_max: float
    solver_tolerance: float
    solver_max_iterations: int


@dataclass(frozen=True, slots=True)
class FusionParameters:
    model_quality_weight: float
    quality_weights: Mapping[InstrumentQuality, float]
    epsilon: float
    interval_z: float
    f1_min_coverage: float
    f1_max_sigma_p95: float
    f2_min_coverage: float
    f2_max_sigma_p95: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "quality_weights",
            MappingProxyType(dict(self.quality_weights)),
        )


@dataclass(frozen=True, slots=True)
class ParameterBundle:
    version: str
    model: ModelParameters
    fusion: FusionParameters
    checksum: str


def load_parameter_bundle(path: str | Path) -> ParameterBundle:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    model = payload["model"]
    fusion = payload["fusion"]

    def axis(name: str) -> AxisParameters:
        item = model[name]
        return AxisParameters(
            intercept=float(item["intercept"]),
            magnitude_coefficient=float(item["magnitude_coefficient"]),
            decay_coefficient=float(item["decay_coefficient"]),
            distance_offset_km=float(item["distance_offset_km"]),
            sigma=float(item["sigma"]),
        )

    weights = {
        InstrumentQuality(code): float(value)
        for code, value in fusion["quality_weights"].items()
    }
    if set(weights) != set(InstrumentQuality):
        raise ValueError("fusion quality weights must define Q1, Q2, Q3 and Q0")

    return ParameterBundle(
        version=str(payload["version"]),
        model=ModelParameters(
            reference_doi=str(model["reference_doi"]),
            long_axis=axis("long_axis"),
            short_axis=axis("short_axis"),
            valid_magnitude_min=float(model["valid_magnitude"]["min"]),
            valid_magnitude_max=float(model["valid_magnitude"]["max"]),
            solver_intensity_min=float(model["solver"]["intensity_min"]),
            solver_intensity_max=float(model["solver"]["intensity_max"]),
            solver_tolerance=float(model["solver"]["tolerance"]),
            solver_max_iterations=int(model["solver"]["max_iterations"]),
        ),
        fusion=FusionParameters(
            model_quality_weight=float(fusion["model_quality_weight"]),
            quality_weights=weights,
            epsilon=float(fusion["epsilon"]),
            interval_z=float(fusion["interval_z"]),
            f1_min_coverage=float(fusion["f1"]["min_coverage"]),
            f1_max_sigma_p95=float(fusion["f1"]["max_sigma_p95"]),
            f2_min_coverage=float(fusion["f2"]["min_coverage"]),
            f2_max_sigma_p95=float(fusion["f2"]["max_sigma_p95"]),
        ),
        checksum=parameter_bundle_checksum(path),
    )


def parameter_bundle_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
