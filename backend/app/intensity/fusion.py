from __future__ import annotations

import math

import numpy as np

from app.intensity.domain import (
    FusionField,
    FusionMode,
    FusionQuality,
    InstrumentProduct,
    InstrumentQuality,
    ModelField,
    ProductStatus,
)
from app.intensity.parameters import FusionParameters


_VALID_INSTRUMENT_QUALITY = {
    InstrumentQuality.Q1.value,
    InstrumentQuality.Q2.value,
    InstrumentQuality.Q3.value,
}


def fuse(
    model: ModelField,
    instrument: InstrumentProduct | None,
    parameters: FusionParameters,
) -> FusionField:
    model_values = np.asarray(model.values, dtype=np.float64)
    model_sigma = np.asarray(model.sigma, dtype=np.float64)
    model_weight = parameters.model_quality_weight / (
        model_sigma**2 + parameters.epsilon
    )
    quality_codes = np.full(
        model_values.shape,
        InstrumentQuality.Q0.value,
        dtype=object,
    )
    instrument_weight = np.zeros_like(model_values)
    fused_values = model_values.copy()
    fused_sigma = model_sigma.copy()
    mode = FusionMode.MODEL_ONLY
    coverage = 0.0

    if (
        instrument is not None
        and instrument.status in {ProductStatus.AVAILABLE, ProductStatus.PARTIAL}
        and instrument.values is not None
        and instrument.sigma is not None
        and instrument.quality_codes is not None
    ):
        quality_codes, valid = _quality_codes(
            model_values.shape,
            instrument.quality_codes,
        )
        q = np.zeros(model_values.shape, dtype=np.float64)
        for quality, weight in parameters.quality_weights.items():
            q[quality_codes == quality.value] = weight
        candidate_weights = q / (instrument.sigma**2 + parameters.epsilon)
        candidate_weights[~valid] = 0.0
        instrument_weight = np.where(
            valid & (candidate_weights > 0),
            candidate_weights,
            0.0,
        )
        total_weight = model_weight + instrument_weight
        fused_candidate = (
            model_weight * model_values + instrument_weight * instrument.values
        ) / total_weight
        fused_values = np.where(
            instrument_weight > 0,
            fused_candidate,
            model_values,
        )
        fused_sigma = np.where(
            instrument_weight > 0,
            np.sqrt(1.0 / total_weight),
            model_sigma,
        )
        coverage = float(
            np.count_nonzero(valid & (q > 0)) / model_values.size
        )
        if bool(instrument_weight.any()):
            mode = FusionMode.FULL

    p10 = fused_values - parameters.interval_z * fused_sigma
    p90 = fused_values + parameters.interval_z * fused_sigma
    quality_sigma = fused_sigma[instrument_weight > 0]
    quality = _fusion_quality(coverage, quality_sigma, mode, parameters)
    return FusionField(
        values=fused_values,
        sigma=fused_sigma,
        p10=p10,
        p90=p90,
        model_weight=model_weight,
        instrument_weight=instrument_weight,
        quality_codes=quality_codes,
        mode=mode,
        quality=quality,
        coverage_ratio=coverage,
    )


def _quality_codes(
    shape: tuple[int, ...],
    source_codes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    flat_codes = source_codes.reshape(-1)
    valid = np.asarray(
        [
            str(code) in _VALID_INSTRUMENT_QUALITY
            for code in flat_codes
        ],
        dtype=bool,
    ).reshape(shape)
    quality_codes = np.full(shape, InstrumentQuality.Q0.value, dtype=object)
    valid_indices = np.flatnonzero(valid)
    quality_codes.flat[valid_indices] = [
        InstrumentQuality(str(flat_codes[index])).value
        for index in valid_indices
    ]
    return quality_codes, valid


def _fusion_quality(
    coverage: float,
    sigma: np.ndarray,
    mode: FusionMode,
    parameters: FusionParameters,
) -> FusionQuality:
    if mode is FusionMode.MODEL_ONLY:
        return FusionQuality.F3
    sigma_p95 = _p95(sigma)
    if (
        coverage >= parameters.f1_min_coverage
        and sigma_p95 <= parameters.f1_max_sigma_p95
    ):
        return FusionQuality.F1
    if (
        coverage >= parameters.f2_min_coverage
        and sigma_p95 <= parameters.f2_max_sigma_p95
    ):
        return FusionQuality.F2
    return FusionQuality.F3


def _p95(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return math.inf
    return float(np.percentile(finite, 95, method="linear"))
