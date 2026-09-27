from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    FusionField,
    FusionMode,
    FusionQuality,
    GridDefinition,
    GridSamples,
    IntensityEventSnapshot,
    InstrumentQuality,
    InstrumentProduct,
    ModelField,
    ProductStatus,
    ProductType,
)


def test_intensity_enums_have_stable_external_values() -> None:
    assert ProductType.MODEL == "model"
    assert ProductType.INSTRUMENT == "instrument"
    assert ProductType.FUSION == "fusion"
    assert ProductStatus.UNAVAILABLE == "unavailable"
    assert InstrumentQuality.Q1 == "Q1"
    assert FusionMode.MODEL_ONLY == "model_only"
    assert FusionQuality.F3 == "F3"
    assert DirectionStatus.UNCERTAIN == "uncertain"


def _assert_read_only(array: np.ndarray) -> None:
    assert array.flags.writeable is False
    with pytest.raises(ValueError, match="read-only"):
        array.flat[0] = 99


def _instrument_product(
    *,
    observed_at: datetime | None = None,
    generated_at: datetime | None = None,
) -> InstrumentProduct:
    return InstrumentProduct(
        status=ProductStatus.AVAILABLE,
        product_id="product-1",
        product_version="1",
        observed_at=observed_at,
        source="test",
        grid_version="grid-1",
        values=np.array([1.0]),
        sigma=np.array([0.1]),
        quality_codes=np.array([1], dtype=np.int8),
        coverage_ratio=1.0,
        generated_at=generated_at,
    )


def test_domain_array_fields_are_read_only_and_do_not_alias_inputs() -> None:
    source_values = np.array([1.0])
    model = ModelField(
        values=source_values,
        sigma=np.array([0.1]),
        extrapolated=False,
        direction=DirectionDecision(
            status=DirectionStatus.RESOLVED,
            source="test",
            strike_deg=90.0,
        ),
    )
    definition = GridDefinition(
        version="grid-1",
        crs="EPSG:32651",
        resolution_m=1_000,
        origin_x=0.0,
        origin_y=0.0,
        width=1,
        height=1,
    )
    samples = GridSamples(
        definition=definition,
        rows=np.array([0], dtype=np.int32),
        columns=np.array([0], dtype=np.int32),
        x_km=np.array([0.5]),
        y_km=np.array([-0.5]),
        longitude=np.array([121.0]),
        latitude=np.array([31.0]),
        distance_km=np.array([0.0]),
        azimuth_deg=np.array([0.0]),
    )
    instrument = _instrument_product(
        observed_at=datetime(2026, 9, 27, tzinfo=UTC),
        generated_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    fusion = FusionField(
        values=np.array([1.0]),
        sigma=np.array([0.1]),
        p10=np.array([0.9]),
        p90=np.array([1.1]),
        model_weight=np.array([1.0]),
        instrument_weight=np.array([0.0]),
        quality_codes=np.array([1], dtype=np.int8),
        mode=FusionMode.MODEL_ONLY,
        quality=FusionQuality.F3,
        coverage_ratio=0.0,
    )

    source_values[0] = 2.0
    assert model.values[0] == 1.0
    for array in (
        model.values,
        model.sigma,
        samples.rows,
        samples.columns,
        samples.x_km,
        samples.y_km,
        samples.longitude,
        samples.latitude,
        samples.distance_km,
        samples.azimuth_deg,
        instrument.values,
        instrument.sigma,
        instrument.quality_codes,
        fusion.values,
        fusion.sigma,
        fusion.p10,
        fusion.p90,
        fusion.model_weight,
        fusion.instrument_weight,
        fusion.quality_codes,
    ):
        assert array is not None
        _assert_read_only(array)


@pytest.mark.parametrize(
    "factory",
    (
        lambda: IntensityEventSnapshot(
            event_id="event-1",
            revision_id="revision-1",
            magnitude=5.0,
            longitude=121.0,
            latitude=31.0,
            report_ingested_at=datetime(2026, 9, 27),
        ),
        lambda: _instrument_product(observed_at=datetime(2026, 9, 27)),
        lambda: _instrument_product(generated_at=datetime(2026, 9, 27)),
    ),
    ids=("report-ingested-at", "observed-at", "generated-at"),
)
def test_domain_timestamps_reject_naive_values(factory) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        factory()


def test_domain_timestamps_accept_aware_values_and_normalize_to_utc() -> None:
    aware = datetime(2026, 9, 27, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    expected = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)

    event = IntensityEventSnapshot(
        event_id="event-1",
        revision_id="revision-1",
        magnitude=5.0,
        longitude=121.0,
        latitude=31.0,
        report_ingested_at=aware,
    )
    instrument = _instrument_product(observed_at=aware, generated_at=aware)

    assert event.report_ingested_at == expected
    assert instrument.observed_at == expected
    assert instrument.generated_at == expected
    assert event.report_ingested_at.tzinfo is UTC
    assert instrument.observed_at is not None
    assert instrument.observed_at.tzinfo is UTC
    assert instrument.generated_at is not None
    assert instrument.generated_at.tzinfo is UTC
