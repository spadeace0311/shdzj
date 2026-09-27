from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridSamples,
    IntensityEventSnapshot,
    ModelField,
)
from app.intensity.parameters import AxisParameters, ModelParameters


class ModelFieldConvergenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FaultCandidate:
    fault_id: str
    strike_deg: float
    distance_km: float


def resolve_direction(
    *,
    override_deg: float | None,
    focal_mechanism_deg: float | None,
    finite_fault_deg: float | None,
    candidates: tuple[FaultCandidate, ...],
) -> DirectionDecision:
    for value, source in (
        (override_deg, "manual_override"),
        (focal_mechanism_deg, "focal_mechanism"),
        (finite_fault_deg, "finite_fault"),
    ):
        if value is not None:
            return DirectionDecision(
                status=DirectionStatus.RESOLVED,
                source=source,
                strike_deg=_normalize_strike(value),
            )
    eligible = tuple(
        candidate for candidate in candidates if candidate.distance_km <= 20.0
    )
    if eligible:
        selected = min(
            eligible,
            key=lambda item: (item.distance_km, item.fault_id),
        )
        return DirectionDecision(
            status=DirectionStatus.RESOLVED,
            source="nearest_fault",
            strike_deg=_normalize_strike(selected.strike_deg),
            candidate_fault_id=selected.fault_id,
            candidate_distance_km=selected.distance_km,
        )
    return DirectionDecision(DirectionStatus.UNCERTAIN, None, None)


def _validated_distance(distance_km: np.ndarray) -> np.ndarray:
    distance = np.asarray(distance_km, dtype=np.float64)
    if np.any(distance < 0):
        raise ValueError("distance_km must not be negative")
    if not np.all(np.isfinite(distance)):
        raise ValueError("distance_km must be finite")
    return distance


def axis_intensity(
    magnitude: float,
    distance_km: np.ndarray,
    axis: AxisParameters,
) -> np.ndarray:
    distance = _validated_distance(distance_km)
    return (
        axis.intercept
        + axis.magnitude_coefficient * magnitude
        - axis.decay_coefficient * np.log(distance + axis.distance_offset_km)
    )


def _axis_radius(
    intensity: float,
    magnitude: float,
    axis: AxisParameters,
) -> float:
    exponent = (
        axis.intercept + axis.magnitude_coefficient * magnitude - intensity
    ) / axis.decay_coefficient
    radius = math.exp(exponent) - axis.distance_offset_km
    if not math.isfinite(radius) or radius <= 0:
        raise ModelFieldConvergenceError("axis radius is outside the valid range")
    return radius


def evaluate_model(
    snapshot: IntensityEventSnapshot,
    samples: GridSamples,
    direction: DirectionDecision,
    parameters: ModelParameters,
) -> ModelField:
    magnitude = float(snapshot.magnitude)
    if not math.isfinite(magnitude):
        raise ValueError("magnitude must be finite")
    _validated_distance(samples.distance_km)
    if direction.status is DirectionStatus.UNCERTAIN:
        long_values = axis_intensity(
            magnitude,
            samples.distance_km,
            parameters.long_axis,
        )
        short_values = axis_intensity(
            magnitude,
            samples.distance_km,
            parameters.short_axis,
        )
        values = (long_values + short_values) / 2.0
        sigma = np.full(
            values.shape,
            math.sqrt(
                (parameters.long_axis.sigma**2 + parameters.short_axis.sigma**2) / 2.0
            ),
            dtype=np.float64,
        )
    else:
        values, sigma = _axis_ratio_field(
            magnitude,
            samples,
            float(direction.strike_deg),
            parameters,
        )
    return ModelField(
        values=values,
        sigma=sigma,
        extrapolated=not (
            parameters.valid_magnitude_min
            <= magnitude
            <= parameters.valid_magnitude_max
        ),
        direction=direction,
    )


def _axis_ratio_field(
    magnitude: float,
    samples: GridSamples,
    strike_deg: float,
    parameters: ModelParameters,
) -> tuple[np.ndarray, np.ndarray]:
    delta = np.deg2rad(samples.azimuth_deg - strike_deg)
    x = samples.distance_km * np.cos(delta)
    y = samples.distance_km * np.sin(delta)
    values = np.empty_like(x)
    sigma = np.empty_like(x)

    center_intensity_long = float(
        axis_intensity(
            magnitude,
            np.asarray([0.0], dtype=np.float64),
            parameters.long_axis,
        )[0]
    )
    center_intensity_short = float(
        axis_intensity(
            magnitude,
            np.asarray([0.0], dtype=np.float64),
            parameters.short_axis,
        )[0]
    )
    field_high_bound = min(
        parameters.solver_intensity_max,
        center_intensity_long - 1e-6,
        center_intensity_short - 1e-6,
    )
    if field_high_bound <= parameters.solver_intensity_min:
        raise ModelFieldConvergenceError("model field has no valid intensity bracket")

    for index, (x_value, y_value, delta_value) in enumerate(zip(x, y, delta, strict=True)):
        distance = float(samples.distance_km[index])
        axis_epsilon = max(distance, 1.0) * 1e-12

        if abs(y_value) <= axis_epsilon:
            values[index] = float(
                axis_intensity(
                    magnitude,
                    np.asarray([abs(x_value)], dtype=np.float64),
                    parameters.long_axis,
                )[0]
            )
            sigma[index] = parameters.long_axis.sigma
            continue

        if abs(x_value) <= axis_epsilon:
            values[index] = float(
                axis_intensity(
                    magnitude,
                    np.asarray([abs(y_value)], dtype=np.float64),
                    parameters.short_axis,
                )[0]
            )
            sigma[index] = parameters.short_axis.sigma
            continue

        low = parameters.solver_intensity_min
        high = field_high_bound
        low_value = _field_residual(
            low,
            magnitude,
            x_value,
            y_value,
            parameters,
        )
        high_value = _field_residual(
            high,
            magnitude,
            x_value,
            y_value,
            parameters,
        )
        if low_value * high_value > 0:
            raise ModelFieldConvergenceError("model field root is not bracketed")
        for _ in range(parameters.solver_max_iterations):
            midpoint = (low + high) / 2.0
            residual = _field_residual(
                midpoint,
                magnitude,
                x_value,
                y_value,
                parameters,
            )
            if (
                abs(residual) <= parameters.solver_tolerance
                or high - low <= parameters.solver_tolerance
            ):
                low = high = midpoint
                break
            if low_value * residual <= 0:
                high = midpoint
            else:
                low = midpoint
                low_value = residual
        else:
            raise ModelFieldConvergenceError("model field bisection exceeded max iterations")

        values[index] = (low + high) / 2.0
        wa = math.cos(delta_value) ** 2
        wb = math.sin(delta_value) ** 2
        sigma[index] = math.sqrt(
            wa * parameters.long_axis.sigma**2
            + wb * parameters.short_axis.sigma**2
        )
    return values, sigma


def _field_residual(
    intensity: float,
    magnitude: float,
    x_km: float,
    y_km: float,
    parameters: ModelParameters,
) -> float:
    long_radius = _axis_radius(intensity, magnitude, parameters.long_axis)
    short_radius = _axis_radius(intensity, magnitude, parameters.short_axis)
    equivalent_distance = math.sqrt(
        x_km**2 + (long_radius / short_radius * y_km) ** 2
    )
    return float(
        axis_intensity(
            magnitude,
            np.asarray([equivalent_distance], dtype=np.float64),
            parameters.long_axis,
        )[0]
        - intensity
    )


def _normalize_strike(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("strike must be finite")
    return value % 360.0
