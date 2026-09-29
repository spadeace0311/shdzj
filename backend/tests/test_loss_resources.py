from math import ceil
from pathlib import Path

import pytest

from app.loss.buildings import BuildingDamageResult
from app.loss.domain import (
    LossModelType,
    LossQualityGrade,
    LossValueType,
    ResourceKind,
    ScenarioParameters,
)
from app.loss.models_registry import load_parameter_set
from app.loss.resources import (
    ResourceDemandUnavailable,
    assess_resource_demand,
)
from tests.loss_factories import (
    building_damage_result,
    casualty_result,
    population_impact_result,
)


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def _central_parameters() -> ScenarioParameters:
    return load_parameter_set(PARAMETERS).models[
        LossModelType.RESOURCE_DEMAND
    ].scenarios[LossValueType.CENTRAL]


def test_all_resource_kinds_are_present_and_integral() -> None:
    result = assess_resource_demand(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        _central_parameters(),
    )

    assert set(result.values) == set(ResourceKind)
    assert all(
        value.quantity is None or value.quantity == int(value.quantity)
        for value in result.values.values()
    )


def test_missing_coefficient_makes_only_that_resource_unavailable() -> None:
    parameters = _central_parameters()
    incomplete = type(parameters)(
        values={
            key: value
            for key, value in parameters.values.items()
            if not key.startswith("tent.")
        }
    )

    result = assess_resource_demand(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        incomplete,
    )

    assert result.values[ResourceKind.TENT].quantity is None
    assert result.values[ResourceKind.TENT].status == "unavailable"
    assert result.values[ResourceKind.TENT].reason == (
        "missing_parameter:tent.base"
    )
    assert result.values[ResourceKind.DRINKING_WATER].quantity is not None


def test_arithmetic_matches_central_parameter_matrix() -> None:
    parameters = _central_parameters()
    result = assess_resource_demand(
        building_damage_result(),
        population_impact_result(
            affected_population=1000.0,
            emergency_shelter_population=100.0,
        ),
        casualty_result(deaths=1.0, injuries=2.0, buried=3.0),
        parameters,
    )
    values = parameters.values

    expected = {
        ResourceKind.RESCUE_TEAM: (
            values["rescue_team.base"]
            + values["rescue_team.rescuer_per_missing"] * 3.0
        ),
        ResourceKind.MEDICAL_TEAM: (
            values["medical_team.base"]
            + values["medical_team.team_per_injury"] * 2.0
        ),
        ResourceKind.EPIDEMIC_TEAM: (
            values["epidemic_team.base"]
            + values["epidemic_team.team_per_shelter_10000"]
            * 100.0
            / 10000.0
        ),
        ResourceKind.TENT: (
            values["tent.base"]
            + values["tent.per_shelter_population"] * 100.0
        ),
        ResourceKind.DRINKING_WATER: (
            values["drinking_water.base"]
            + values["drinking_water.per_affected_population"] * 1000.0
        ),
        ResourceKind.TOILET: (
            values["toilet.base"]
            + values["toilet.per_shelter_population"] * 100.0
        ),
        ResourceKind.CLOTHING: (
            values["clothing.base"]
            + values["clothing.per_affected_population"] * 1000.0
        ),
        ResourceKind.QUILT: (
            values["quilt.base"]
            + values["quilt.per_shelter_population"] * 100.0
        ),
        ResourceKind.FOOD: (
            values["food.base"]
            + values["food.per_affected_population"] * 1000.0
        ),
        ResourceKind.BLANKET: (
            values["blanket.base"]
            + values["blanket.per_affected_population"] * 1000.0
        ),
        ResourceKind.STRETCHER: (
            values["stretcher.base"]
            + values["stretcher.per_injury"] * 2.0
        ),
        ResourceKind.SICKBED: (
            values["sickbed.base"]
            + values["sickbed.per_injury"] * 2.0
        ),
    }

    assert result.inputs.affected_population == pytest.approx(1000.0)
    assert result.inputs.emergency_shelter_population == pytest.approx(100.0)
    assert result.inputs.deaths == pytest.approx(1.0)
    assert result.inputs.injuries == pytest.approx(2.0)
    assert result.inputs.buried == pytest.approx(3.0)
    for kind, raw in expected.items():
        assert result.values[kind].quantity == pytest.approx(
            ceil(max(raw, 0.0))
        )
        assert result.values[kind].status == "available"
        assert result.values[kind].reason is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tent.base", float("nan")),
        ("tent.base", float("inf")),
        ("tent.base", -1.0),
        ("tent.base", "not-a-number"),
    ],
)
def test_invalid_coefficient_makes_only_that_resource_unavailable(
    key: str,
    value: object,
) -> None:
    parameters = _central_parameters()
    parameters.values[key] = value

    result = assess_resource_demand(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        parameters,
    )

    assert result.values[ResourceKind.TENT].quantity is None
    assert result.values[ResourceKind.TENT].status == "unavailable"
    assert result.values[ResourceKind.TENT].reason is not None
    assert result.values[ResourceKind.DRINKING_WATER].quantity is not None


def test_invalid_aggregate_casualty_input_is_unavailable() -> None:
    with pytest.raises(ResourceDemandUnavailable, match="buried"):
        assess_resource_demand(
            building_damage_result(),
            population_impact_result(),
            casualty_result(buried=float("nan")),
            _central_parameters(),
        )


def test_empty_coverage_is_unavailable() -> None:
    with pytest.raises(ResourceDemandUnavailable, match="town"):
        assess_resource_demand(
            BuildingDamageResult(
                towns={},
                quality_grade=LossQualityGrade.L2,
                fallback_model_used=False,
            ),
            population_impact_result(),
            casualty_result(),
            _central_parameters(),
        )
