from pathlib import Path

import pytest

from app.loss.domain import LossModelType, LossValueType
from app.loss.models_registry import (
    LossModelRegistry,
    LossParametersUnavailable,
    load_parameter_set,
    require_parameters,
)


PRODUCTION_PARAMETERS = Path("/config/loss/shanghai-reference-uncalibrated.yaml")
TEST_PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def test_production_parameter_set_is_explicitly_uncalibrated() -> None:
    parameter_set = load_parameter_set(PRODUCTION_PARAMETERS)
    assert parameter_set.version == "shanghai-loss-reference-v1"
    assert parameter_set.calibration_status.value == "reference_uncalibrated"
    assert parameter_set.provenance["requires_review"] is True
    assert parameter_set.models[LossModelType.BUILDING_DAMAGE].source_requirements


def test_missing_numeric_parameter_is_unavailable_not_zero() -> None:
    parameter_set = load_parameter_set(PRODUCTION_PARAMETERS)
    with pytest.raises(LossParametersUnavailable):
        require_parameters(
            parameter_set,
            LossModelType.BUILDING_DAMAGE,
            LossValueType.CENTRAL,
            ("vulnerability.rc_frame.7.slightly_damaged",),
        )


def test_registry_resolves_default_and_version() -> None:
    registry = LossModelRegistry()
    registry.register_defaults(load_parameter_set(TEST_PARAMETERS))
    definition = registry.resolve(
        LossModelType.BUILDING_DAMAGE,
        "building-structure-matrix-v1",
    )
    assert definition.is_default is True
    assert definition.formula_version == "building-structure-matrix-v1"
