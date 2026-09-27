from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    FusionMode,
    FusionQuality,
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ModelField,
    ProductStatus,
)
from app.intensity.fusion import fuse
from app.intensity.parameters import load_parameter_bundle


PARAMETERS = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")


def _model(values: list[float], sigma: list[float]) -> ModelField:
    return ModelField(
        values=np.asarray(values, dtype=np.float64),
        sigma=np.asarray(sigma, dtype=np.float64),
        extrapolated=False,
        direction=DirectionDecision(DirectionStatus.UNCERTAIN, None, None),
    )


def _instrument(
    values: np.ndarray,
    sigma: np.ndarray,
    quality_codes: np.ndarray,
    *,
    coverage_ratio: float = 1.0,
) -> InstrumentProduct:
    return InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="i",
        product_version="v1",
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        source="fixture",
        format=InstrumentProductFormat.GRID,
        source_verified=True,
        grid_version="g",
        values=values,
        sigma=sigma,
        quality_codes=quality_codes,
        coverage_ratio=coverage_ratio,
    )


def test_missing_instrument_returns_model_only() -> None:
    result = fuse(
        _model([4.0, 5.0], [0.6, 0.7]),
        None,
        PARAMETERS.fusion,
    )

    assert result.mode is FusionMode.MODEL_ONLY
    assert result.quality is FusionQuality.F3
    assert result.coverage_ratio == 0.0
    assert result.values.tolist() == [4.0, 5.0]
    assert result.instrument_weight.tolist() == [0.0, 0.0]


def test_inverse_variance_weighting_matches_expected() -> None:
    instrument = _instrument(
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.2], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q1], dtype=object),
    )
    result = fuse(_model([4.0], [1.0]), instrument, PARAMETERS.fusion)
    model_weight = 1.0 / (1.0**2 + PARAMETERS.fusion.epsilon)
    instrument_weight = 1.0 / (0.2**2 + PARAMETERS.fusion.epsilon)
    expected = (model_weight * 4.0 + instrument_weight * 6.0) / (
        model_weight + instrument_weight
    )

    assert result.values[0] == pytest.approx(expected, abs=1e-6)
    assert result.p10[0] <= result.values[0] <= result.p90[0]
    assert result.mode is FusionMode.FULL


def test_quality_thresholds_follow_coverage_and_sigma() -> None:
    instrument = _instrument(
        values=np.zeros(100, dtype=np.float64),
        sigma=np.full(100, 0.1, dtype=np.float64),
        quality_codes=np.full(100, InstrumentQuality.Q1, dtype=object),
    )
    result = fuse(
        _model([5.0] * 100, [0.5] * 100),
        instrument,
        PARAMETERS.fusion,
    )

    assert result.quality is FusionQuality.F1


def test_all_zero_quality_codes_fall_back_to_model_only() -> None:
    instrument = _instrument(
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.1], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q0], dtype=object),
        coverage_ratio=0.0,
    )

    result = fuse(_model([4.0], [0.5]), instrument, PARAMETERS.fusion)

    assert result.mode is FusionMode.MODEL_ONLY
    assert result.values.tolist() == [4.0]
    assert result.instrument_weight.tolist() == [0.0]
