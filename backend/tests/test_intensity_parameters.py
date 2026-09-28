from pathlib import Path

import pytest

from app.intensity.domain import InstrumentQuality
from app.intensity.parameters import (
    FusionParameters,
    load_parameter_bundle,
    parameter_bundle_checksum,
)


PARAMETERS = Path("/config/intensity/shanghai-2019.yaml")


def test_loads_approved_model_and_fusion_parameters() -> None:
    bundle = load_parameter_bundle(PARAMETERS)

    assert bundle.version == "shanghai-2019.1"
    assert bundle.checksum == parameter_bundle_checksum(PARAMETERS)
    assert bundle.model.long_axis.intercept == pytest.approx(3.4142)
    assert bundle.model.long_axis.decay_coefficient == pytest.approx(0.96272)
    assert bundle.model.short_axis.sigma == pytest.approx(0.6638)
    assert bundle.model.valid_magnitude_min == pytest.approx(4.0)
    assert bundle.model.valid_magnitude_max == pytest.approx(6.2)
    assert bundle.fusion.model_quality_weight == pytest.approx(1.0)
    assert bundle.fusion.quality_weights[InstrumentQuality.Q2] == pytest.approx(0.5)
    assert bundle.fusion.f1_min_coverage == pytest.approx(0.90)


def test_parameter_checksum_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    source = PARAMETERS.read_text(encoding="utf-8")
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(source, encoding="utf-8")
    second.write_text(source.replace("0.6310", "0.6311"), encoding="utf-8")

    assert parameter_bundle_checksum(first) == parameter_bundle_checksum(PARAMETERS)
    assert parameter_bundle_checksum(second) != parameter_bundle_checksum(PARAMETERS)


def test_fusion_quality_weights_are_immutable_and_do_not_alias_inputs() -> None:
    source = {
        InstrumentQuality.Q1: 1.0,
        InstrumentQuality.Q2: 0.5,
        InstrumentQuality.Q3: 0.25,
        InstrumentQuality.Q0: 0.0,
    }
    parameters = FusionParameters(
        model_quality_weight=1.0,
        quality_weights=source,
        epsilon=0.000001,
        interval_z=1.2816,
        f1_min_coverage=0.90,
        f1_max_sigma_p95=0.75,
        f2_min_coverage=0.50,
        f2_max_sigma_p95=1.25,
    )

    source[InstrumentQuality.Q1] = 0.0
    assert parameters.quality_weights[InstrumentQuality.Q1] == 1.0
    with pytest.raises(TypeError):
        parameters.quality_weights[InstrumentQuality.Q1] = 0.0
