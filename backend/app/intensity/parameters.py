from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from app.intensity.domain import InstrumentQuality


def _finite_float(
    value: object,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strictly_positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and converted < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and converted > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    if strictly_positive and converted <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and converted < 0:
        raise ValueError(f"{field} must not be negative")
    return converted


@dataclass(frozen=True, slots=True)
class AxisParameters:
    intercept: float
    magnitude_coefficient: float
    decay_coefficient: float
    distance_offset_km: float
    sigma: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intercept",
            _finite_float(self.intercept, "axis intercept"),
        )
        object.__setattr__(
            self,
            "magnitude_coefficient",
            _finite_float(
                self.magnitude_coefficient,
                "axis magnitude_coefficient",
                strictly_positive=True,
            ),
        )
        object.__setattr__(
            self,
            "decay_coefficient",
            _finite_float(
                self.decay_coefficient,
                "axis decay_coefficient",
                strictly_positive=True,
            ),
        )
        object.__setattr__(
            self,
            "distance_offset_km",
            _finite_float(
                self.distance_offset_km,
                "axis distance_offset_km",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "sigma",
            _finite_float(
                self.sigma,
                "axis sigma",
                nonnegative=True,
            ),
        )


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

    def __post_init__(self) -> None:
        if not str(self.reference_doi).strip():
            raise ValueError("reference_doi must not be empty")
        object.__setattr__(self, "reference_doi", str(self.reference_doi).strip())
        object.__setattr__(
            self,
            "valid_magnitude_min",
            _finite_float(
                self.valid_magnitude_min,
                "model valid_magnitude_min",
            ),
        )
        object.__setattr__(
            self,
            "valid_magnitude_max",
            _finite_float(
                self.valid_magnitude_max,
                "model valid_magnitude_max",
            ),
        )
        if self.valid_magnitude_min > self.valid_magnitude_max:
            raise ValueError("valid magnitude range must be non-empty")
        object.__setattr__(
            self,
            "solver_intensity_min",
            _finite_float(
                self.solver_intensity_min,
                "model solver_intensity_min",
            ),
        )
        object.__setattr__(
            self,
            "solver_intensity_max",
            _finite_float(
                self.solver_intensity_max,
                "model solver_intensity_max",
            ),
        )
        if self.solver_intensity_min >= self.solver_intensity_max:
            raise ValueError("solver intensity range must be non-empty")
        object.__setattr__(
            self,
            "solver_tolerance",
            _finite_float(
                self.solver_tolerance,
                "model solver_tolerance",
                strictly_positive=True,
            ),
        )
        if isinstance(self.solver_max_iterations, bool) or not isinstance(
            self.solver_max_iterations,
            int,
        ):
            raise ValueError("solver_max_iterations must be an integer")
        if self.solver_max_iterations < 1:
            raise ValueError("solver_max_iterations must be positive")


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
            "model_quality_weight",
            _finite_float(
                self.model_quality_weight,
                "fusion model_quality_weight",
                strictly_positive=True,
            ),
        )
        weights = {
            InstrumentQuality(key): _finite_float(
                value,
                f"fusion quality weight {key}",
                minimum=0.0,
                maximum=1.0,
            )
            for key, value in self.quality_weights.items()
        }
        if set(weights) != set(InstrumentQuality):
            raise ValueError("fusion quality weights must define Q1, Q2, Q3 and Q0")
        object.__setattr__(
            self,
            "epsilon",
            _finite_float(
                self.epsilon,
                "fusion epsilon",
                strictly_positive=True,
            ),
        )
        object.__setattr__(
            self,
            "interval_z",
            _finite_float(
                self.interval_z,
                "fusion interval_z",
                strictly_positive=True,
            ),
        )
        object.__setattr__(
            self,
            "f1_min_coverage",
            _finite_float(
                self.f1_min_coverage,
                "fusion f1_min_coverage",
                minimum=0.0,
                maximum=1.0,
            ),
        )
        object.__setattr__(
            self,
            "f1_max_sigma_p95",
            _finite_float(
                self.f1_max_sigma_p95,
                "fusion f1_max_sigma_p95",
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "f2_min_coverage",
            _finite_float(
                self.f2_min_coverage,
                "fusion f2_min_coverage",
                minimum=0.0,
                maximum=1.0,
            ),
        )
        object.__setattr__(
            self,
            "f2_max_sigma_p95",
            _finite_float(
                self.f2_max_sigma_p95,
                "fusion f2_max_sigma_p95",
                nonnegative=True,
            ),
        )
        if self.f1_min_coverage < self.f2_min_coverage:
            raise ValueError("F1 coverage threshold must be at least F2 coverage")
        if self.f1_max_sigma_p95 > self.f2_max_sigma_p95:
            raise ValueError("F1 sigma threshold must not exceed F2 sigma")
        object.__setattr__(
            self,
            "quality_weights",
            MappingProxyType(weights),
        )


@dataclass(frozen=True, slots=True)
class ParameterBundle:
    version: str
    model: ModelParameters
    fusion: FusionParameters
    checksum: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("parameter bundle version must not be empty")
        if len(self.checksum) != 64:
            raise ValueError("parameter checksum must be a SHA-256 hex digest")
        object.__setattr__(self, "version", self.version.strip())


def load_parameter_bundle(path: str | Path) -> ParameterBundle:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("parameter bundle root must be a mapping")
    if not isinstance(payload.get("model"), dict) or not isinstance(
        payload.get("fusion"),
        dict,
    ):
        raise ValueError("parameter bundle requires model and fusion mappings")
    model = payload["model"]
    fusion = payload["fusion"]

    def axis(name: str) -> AxisParameters:
        item = model[name]
        return AxisParameters(
            intercept=_finite_float(item["intercept"], f"model {name} intercept"),
            magnitude_coefficient=_finite_float(
                item["magnitude_coefficient"],
                f"model {name} magnitude_coefficient",
            ),
            decay_coefficient=_finite_float(
                item["decay_coefficient"],
                f"model {name} decay_coefficient",
            ),
            distance_offset_km=_finite_float(
                item["distance_offset_km"],
                f"model {name} distance_offset_km",
            ),
            sigma=_finite_float(item["sigma"], f"model {name} sigma"),
        )

    weights = {
        InstrumentQuality(code): _finite_float(
            value,
            f"fusion quality weight {code}",
        )
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
            valid_magnitude_min=_finite_float(
                model["valid_magnitude"]["min"],
                "model valid_magnitude_min",
            ),
            valid_magnitude_max=_finite_float(
                model["valid_magnitude"]["max"],
                "model valid_magnitude_max",
            ),
            solver_intensity_min=_finite_float(
                model["solver"]["intensity_min"],
                "model solver_intensity_min",
            ),
            solver_intensity_max=_finite_float(
                model["solver"]["intensity_max"],
                "model solver_intensity_max",
            ),
            solver_tolerance=_finite_float(
                model["solver"]["tolerance"],
                "model solver_tolerance",
            ),
            solver_max_iterations=int(model["solver"]["max_iterations"]),
        ),
        fusion=FusionParameters(
            model_quality_weight=_finite_float(
                fusion["model_quality_weight"],
                "fusion model_quality_weight",
            ),
            quality_weights=weights,
            epsilon=_finite_float(fusion["epsilon"], "fusion epsilon"),
            interval_z=_finite_float(fusion["interval_z"], "fusion interval_z"),
            f1_min_coverage=_finite_float(
                fusion["f1"]["min_coverage"],
                "fusion f1_min_coverage",
            ),
            f1_max_sigma_p95=_finite_float(
                fusion["f1"]["max_sigma_p95"],
                "fusion f1_max_sigma_p95",
            ),
            f2_min_coverage=_finite_float(
                fusion["f2"]["min_coverage"],
                "fusion f2_min_coverage",
            ),
            f2_max_sigma_p95=_finite_float(
                fusion["f2"]["max_sigma_p95"],
                "fusion f2_max_sigma_p95",
            ),
        ),
        checksum=parameter_bundle_checksum(path),
    )


def parameter_bundle_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
