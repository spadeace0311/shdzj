from pathlib import Path

import pytest

from app.loss.buildings import (
    BuildingDamageResult,
    BuildingDamageStateArea,
    TownBuildingDamage,
)
from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossModelType,
    LossQualityGrade,
    LossValueType,
    ScenarioParameters,
)
from app.loss.economic import EconomicLossUnavailable, assess_economic_loss
from app.loss.models_registry import load_parameter_set
from tests.loss_factories import building_damage_result as _building_result


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")

EXCLUDED_SCOPE = (
    "transport",
    "lifelines",
    "business_interruption",
    "indirect_loss",
    "recovery_duration",
)


def _central_parameters() -> ScenarioParameters:
    return load_parameter_set(PARAMETERS).models[
        LossModelType.ECONOMIC_LOSS
    ].scenarios[LossValueType.CENTRAL]


def _town(
    *,
    town_code: str,
    states: tuple[BuildingDamageStateArea, ...],
) -> TownBuildingDamage:
    total_area_m2 = 0.0
    for state in states:
        try:
            total_area_m2 += float(state.area_m2)
        except (TypeError, ValueError):
            pass
    return TownBuildingDamage(
        town_code=town_code,
        total_area_m2=total_area_m2,
        states=states,
        quality_grade=LossQualityGrade.L3,
    )


def _state(
    *,
    structure: BuildingStructure = BuildingStructure.RC_FRAME,
    intensity_bin: int = 7,
    damage_state: DamageState = DamageState.COLLAPSED,
    area_m2: float = 100.0,
) -> BuildingDamageStateArea:
    return BuildingDamageStateArea(
        structure=structure,
        intensity_bin=intensity_bin,
        damage_state=damage_state,
        area_m2=area_m2,
    )


def test_economic_loss_is_partial_and_equals_components() -> None:
    parameters = _central_parameters()
    result = assess_economic_loss(_building_result(), parameters)
    town = result.towns["t1"]
    assert result.partial_scope is True
    assert result.excluded_scope == EXCLUDED_SCOPE
    assert town.reconstruction_loss_yuan >= 0
    assert town.contents_loss_yuan >= 0
    assert town.total_loss_yuan == pytest.approx(
        town.reconstruction_loss_yuan + town.contents_loss_yuan
    )


def test_higher_damage_increases_economic_loss() -> None:
    parameters = _central_parameters()
    low = assess_economic_loss(
        _building_result(severe_fraction=0.05),
        parameters,
    )
    high = assess_economic_loss(
        _building_result(severe_fraction=0.20),
        parameters,
    )
    assert high.towns["t1"].total_loss_yuan > low.towns["t1"].total_loss_yuan


def test_arithmetic_uses_parameter_matrix() -> None:
    parameters = _central_parameters()
    result = assess_economic_loss(
        _building_result(severe_fraction=0.05),
        parameters,
    )
    town = result.towns["t1"]

    expected_reconstruction = sum(
        state.area_m2
        * parameters.values[
            f"loss_ratio.{state.structure.value}.{state.damage_state.value}"
        ]
        * parameters.values[
            f"replacement_cost_yuan_m2.{state.structure.value}"
        ]
        for state in _building_result(severe_fraction=0.05).towns["t1"].states
    )
    expected_contents = sum(
        state.area_m2
        * parameters.values[
            f"contents_ratio.{state.structure.value}.{state.damage_state.value}"
        ]
        * parameters.values[
            f"contents_value_yuan_m2.{state.structure.value}"
        ]
        for state in _building_result(severe_fraction=0.05).towns["t1"].states
    )
    assert town.reconstruction_loss_yuan == pytest.approx(
        expected_reconstruction
    )
    assert town.contents_loss_yuan == pytest.approx(expected_contents)
    assert town.total_loss_yuan == pytest.approx(
        expected_reconstruction + expected_contents
    )


def test_result_is_deterministic_by_town_order() -> None:
    parameters = _central_parameters()
    buildings = BuildingDamageResult(
        towns={
            "t2": _town(
                town_code="t2",
                states=(_state(),),
            ),
            "t1": _town(
                town_code="t1",
                states=(_state(area_m2=200.0),),
            ),
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=False,
    )
    result = assess_economic_loss(buildings, parameters)
    assert list(result.towns) == ["t1", "t2"]


@pytest.mark.parametrize(
    "key",
    [
        "replacement_cost_yuan_m2.rc_frame",
        "contents_value_yuan_m2.rc_frame",
        "loss_ratio.rc_frame.basic",
        "loss_ratio.rc_frame.collapsed",
        "contents_ratio.rc_frame.basic",
        "contents_ratio.rc_frame.collapsed",
    ],
)
def test_missing_required_parameter_is_unavailable(key: str) -> None:
    parameters = _central_parameters()
    del parameters.values[key]
    with pytest.raises(EconomicLossUnavailable, match=key):
        assess_economic_loss(_building_result(), parameters)


@pytest.mark.parametrize(
    "key",
    [
        "replacement_cost_yuan_m2.rc_frame",
        "contents_value_yuan_m2.rc_frame",
        "loss_ratio.rc_frame.collapsed",
        "contents_ratio.rc_frame.collapsed",
    ],
)
def test_non_numeric_parameter_is_unavailable(key: str) -> None:
    parameters = _central_parameters()
    parameters.values[key] = "not-a-number"
    with pytest.raises(EconomicLossUnavailable, match=key):
        assess_economic_loss(_building_result(), parameters)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("replacement_cost_yuan_m2.rc_frame", float("nan")),
        ("replacement_cost_yuan_m2.rc_frame", float("inf")),
        ("contents_value_yuan_m2.rc_frame", float("nan")),
        ("loss_ratio.rc_frame.collapsed", float("nan")),
        ("loss_ratio.rc_frame.collapsed", float("inf")),
        ("contents_ratio.rc_frame.collapsed", float("-inf")),
    ],
)
def test_non_finite_parameter_is_unavailable(key: str, value: float) -> None:
    parameters = _central_parameters()
    parameters.values[key] = value
    with pytest.raises(EconomicLossUnavailable, match=key):
        assess_economic_loss(_building_result(), parameters)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("loss_ratio.rc_frame.collapsed", -0.01),
        ("loss_ratio.rc_frame.collapsed", 1.01),
        ("contents_ratio.rc_frame.collapsed", -0.01),
        ("contents_ratio.rc_frame.collapsed", 1.01),
        ("replacement_cost_yuan_m2.rc_frame", -1.0),
        ("contents_value_yuan_m2.rc_frame", -1.0),
    ],
)
def test_out_of_range_parameter_is_unavailable(key: str, value: float) -> None:
    parameters = _central_parameters()
    parameters.values[key] = value
    with pytest.raises(EconomicLossUnavailable, match=key):
        assess_economic_loss(_building_result(), parameters)


@pytest.mark.parametrize(
    "area_m2",
    [
        -1.0,
        float("nan"),
        float("inf"),
        "not-an-area",
    ],
)
def test_invalid_area_is_unavailable(area_m2: object) -> None:
    parameters = _central_parameters()
    buildings = BuildingDamageResult(
        towns={
            "t1": _town(
                town_code="t1",
                states=(_state(area_m2=area_m2),),
            )
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=False,
    )
    with pytest.raises(EconomicLossUnavailable, match="area"):
        assess_economic_loss(buildings, parameters)
