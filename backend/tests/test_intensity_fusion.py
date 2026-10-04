from dataclasses import replace
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
from app.intensity.fusion import _fusion_quality, fuse
from app.intensity.parameters import FusionParameters, load_parameter_bundle


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
    status: ProductStatus = ProductStatus.AVAILABLE,
    coverage_ratio: float = 1.0,
) -> InstrumentProduct:
    return InstrumentProduct(
        status=status,
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


def _fusion_parameters(**overrides: object) -> FusionParameters:
    return replace(PARAMETERS.fusion, **overrides)


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


@pytest.mark.parametrize(
    "status",
    (ProductStatus.UNAVAILABLE, ProductStatus.INVALID, ProductStatus.STALE),
)
def test_non_fusion_statuses_fall_back_to_model_only(status: ProductStatus) -> None:
    instrument = _instrument(
        status=status,
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.2], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q1], dtype=object),
    )

    result = fuse(_model([4.0], [0.5]), instrument, PARAMETERS.fusion)

    assert result.mode is FusionMode.MODEL_ONLY
    assert result.quality is FusionQuality.F3
    assert result.values.tolist() == [4.0]
    assert result.instrument_weight.tolist() == [0.0]
    assert result.coverage_ratio == 0.0


def test_partial_instrument_uses_model_only_outside_valid_cells() -> None:
    instrument = _instrument(
        values=np.array([6.0, 7.0], dtype=np.float64),
        sigma=np.array([0.2, 0.2], dtype=np.float64),
        quality_codes=np.array(
            [InstrumentQuality.Q1, InstrumentQuality.Q0],
            dtype=object,
        ),
    )

    result = fuse(
        _model([4.0, 5.0], [0.5, 0.5]),
        instrument,
        PARAMETERS.fusion,
    )

    assert result.mode is FusionMode.FULL
    assert result.coverage_ratio == pytest.approx(0.5)
    assert result.values[1] == pytest.approx(5.0)
    assert result.instrument_weight[0] > 0.0
    assert result.instrument_weight[1] == 0.0
    assert result.quality is FusionQuality.F2


def test_zero_weight_cell_ignores_non_finite_instrument_value() -> None:
    instrument = _instrument(
        values=np.array([np.nan], dtype=np.float64),
        sigma=np.array([0.1], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q0], dtype=object),
        coverage_ratio=0.0,
    )

    result = fuse(_model([4.0], [0.5]), instrument, PARAMETERS.fusion)

    assert result.values[0] == 4.0
    assert result.instrument_weight[0] == 0.0
    assert result.quality_codes[0] == InstrumentQuality.Q0.value


def test_coverage_counts_only_valid_codes_with_positive_quality_weight() -> None:
    parameters = _fusion_parameters(
        quality_weights={
            InstrumentQuality.Q1: 1.0,
            InstrumentQuality.Q2: 0.0,
            InstrumentQuality.Q3: 0.25,
            InstrumentQuality.Q0: 0.0,
        }
    )
    instrument = _instrument(
        values=np.array([6.0, 6.0], dtype=np.float64),
        sigma=np.array([0.2, 0.2], dtype=np.float64),
        quality_codes=np.array(
            [InstrumentQuality.Q1, InstrumentQuality.Q2],
            dtype=object,
        ),
    )

    result = fuse(
        _model([4.0, 4.0], [0.5, 0.5]),
        instrument,
        parameters,
    )

    assert result.coverage_ratio == pytest.approx(0.5)
    assert result.instrument_weight[0] > 0.0
    assert result.instrument_weight[1] == 0.0


@pytest.mark.parametrize(
    ("quality", "quality_weight"),
    (
        (InstrumentQuality.Q2, 0.5),
        (InstrumentQuality.Q3, 0.25),
    ),
)
def test_q2_and_q3_inverse_variance_weighting(
    quality: InstrumentQuality,
    quality_weight: float,
) -> None:
    instrument = _instrument(
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([1.0], dtype=np.float64),
        quality_codes=np.array([quality], dtype=object),
    )

    result = fuse(_model([4.0], [1.0]), instrument, PARAMETERS.fusion)

    expected = (1.0 * 4.0 + quality_weight * 6.0) / (1.0 + quality_weight)
    assert result.values[0] == pytest.approx(expected, abs=1e-10)
    assert result.instrument_weight[0] == pytest.approx(
        quality_weight / (1.0 + PARAMETERS.fusion.epsilon),
        abs=1e-12,
    )


def test_quality_weight_mapping_order_is_independent() -> None:
    reordered_parameters = _fusion_parameters(
        quality_weights={
            InstrumentQuality.Q3: 0.25,
            InstrumentQuality.Q0: 0.0,
            InstrumentQuality.Q2: 0.5,
            InstrumentQuality.Q1: 1.0,
        }
    )
    instrument = _instrument(
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.2], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q1], dtype=object),
    )

    expected = fuse(_model([4.0], [1.0]), instrument, PARAMETERS.fusion)
    reordered = fuse(
        _model([4.0], [1.0]),
        instrument,
        reordered_parameters,
    )

    assert np.allclose(reordered.values, expected.values)
    assert np.allclose(reordered.instrument_weight, expected.instrument_weight)


def test_f1_f2_f3_quality_boundaries_are_inclusive_and_ordered() -> None:
    parameters = _fusion_parameters(
        f1_min_coverage=0.90,
        f1_max_sigma_p95=0.75,
        f2_min_coverage=0.50,
        f2_max_sigma_p95=1.25,
    )

    assert _fusion_quality(
        0.90,
        np.array([0.75]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F1
    assert _fusion_quality(
        0.899999,
        np.array([0.75]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F2
    assert _fusion_quality(
        0.90,
        np.array([0.750001]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F2
    assert _fusion_quality(
        0.50,
        np.array([1.25]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F2
    assert _fusion_quality(
        0.499999,
        np.array([1.25]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F3
    assert _fusion_quality(
        0.50,
        np.array([1.250001]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F3
    assert _fusion_quality(
        1.00,
        np.array([1.250001]),
        FusionMode.FULL,
        parameters,
    ) is FusionQuality.F3


def test_quality_codes_are_normalized_strings_not_enum_objects() -> None:
    instrument = _instrument(
        values=np.array([6.0], dtype=np.float64),
        sigma=np.array([0.2], dtype=np.float64),
        quality_codes=np.array([InstrumentQuality.Q1], dtype=object),
    )

    result = fuse(_model([4.0], [1.0]), instrument, PARAMETERS.fusion)

    assert result.quality_codes[0] == InstrumentQuality.Q1.value
    assert not isinstance(result.quality_codes[0], InstrumentQuality)
