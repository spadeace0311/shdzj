from pathlib import Path

import pytest

from app.loss.buildings import assess_building_damage
from app.loss.domain import (
    DamageState,
    LossModelType,
    LossQualityGrade,
    LossValueType,
    ScenarioParameters,
)
from app.loss.exposure import build_exposure_dataset
from app.loss.models_registry import load_parameter_set
from app.loss.spatial import TownIntensityShare
from tests.loss_factories import building_damage_result


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def _parameters(
    *,
    structure: str = "rc_frame",
    era: str | None = None,
    intensity_bin: int = 7,
    fractions: tuple[float, ...] = (0.60, 0.20, 0.12, 0.06, 0.02),
) -> ScenarioParameters:
    states = (
        "basic",
        "slightly_damaged",
        "moderately_damaged",
        "severely_damaged",
        "collapsed",
    )
    prefix = "vulnerability"
    if era is None:
        segments = (prefix, structure, str(intensity_bin))
    else:
        segments = (prefix, structure, era, str(intensity_bin))
    return ScenarioParameters(
        values={
            ".".join((*segments, state)): fraction
            for state, fraction in zip(states, fractions, strict=True)
        }
    )


def _exposure(
    *,
    town_code: str = "t1",
    structure: str = "rc_frame",
    era: str | None = None,
    area_m2: float = 10000.0,
) -> object:
    return build_exposure_dataset(
        snapshot_checksum="a" * 64,
        towns=[
            {
                "town_code": town_code,
                "county_code": "c1",
                "town_name": "测试镇",
                "population_total": 1000.0,
            }
        ],
        geometries=[
            {
                "town_code": town_code,
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            }
        ],
        buildings=[
            {
                "town_code": town_code,
                "structure": structure,
                "era": era,
                "area_m2": area_m2,
            }
        ],
    )


def _share(
    *,
    town_code: str = "t1",
    intensity_bin: int = 7,
    area_ratio: float = 1.0,
) -> TownIntensityShare:
    return TownIntensityShare(
        town_code=town_code,
        intensity_bin=intensity_bin,
        area_ratio=area_ratio,
        intensity_min=intensity_bin - 0.5,
        intensity_max=intensity_bin + 0.5,
    )


def test_building_damage_preserves_area_and_returns_all_states() -> None:
    parameters = load_parameter_set(PARAMETERS).models[
        LossModelType.BUILDING_DAMAGE
    ].scenarios[LossValueType.CENTRAL]
    result = assess_building_damage(
        _exposure(),
        (_share(),),
        parameters,
    )
    town = result.towns["t1"]
    assert sum(state.area_m2 for state in town.states) == pytest.approx(10000.0)
    assert {state.damage_state.value for state in town.states} == {
        "basic",
        "slightly_damaged",
        "moderately_damaged",
        "severely_damaged",
        "collapsed",
    }


def test_missing_matrix_row_is_an_error() -> None:
    parameters = load_parameter_set(PARAMETERS).models[
        LossModelType.BUILDING_DAMAGE
    ].scenarios[LossValueType.CENTRAL]
    with pytest.raises(KeyError):
        assess_building_damage(
            _exposure(),
            (_share(intensity_bin=12),),
            parameters,
        )


def test_no_era_fallback_marks_town_and_result_l3() -> None:
    result = assess_building_damage(
        _exposure(era=None),
        (_share(),),
        _parameters(era=None),
    )
    town = result.towns["t1"]
    assert town.quality_grade is LossQualityGrade.L3
    assert result.quality_grade is LossQualityGrade.L3
    assert result.fallback_model_used is True


def test_era_specific_row_is_preferred_without_fallback() -> None:
    result = assess_building_damage(
        _exposure(era="2020"),
        (_share(),),
        _parameters(era="2020", fractions=(0.5, 0.2, 0.1, 0.1, 0.1)),
    )
    town = result.towns["t1"]
    collapsed = next(
        state
        for state in town.states
        if state.damage_state is DamageState.COLLAPSED
    )
    assert collapsed.area_m2 == pytest.approx(1000.0)
    assert town.quality_grade is LossQualityGrade.L2
    assert result.quality_grade is LossQualityGrade.L2
    assert result.fallback_model_used is False


def test_missing_era_specific_row_does_not_fall_back() -> None:
    parameters = _parameters(era=None)
    with pytest.raises(KeyError):
        assess_building_damage(
            _exposure(era="2020"),
            (_share(),),
            parameters,
        )


def test_downscales_only_when_states_exceed_available_area() -> None:
    result = assess_building_damage(
        _exposure(area_m2=1000.0),
        (_share(area_ratio=0.5),),
        _parameters(fractions=(0.80, 0.70, 0.50, 0.40, 0.10)),
    )
    town = result.towns["t1"]
    assert town.total_area_m2 == pytest.approx(500.0)
    assert sum(state.area_m2 for state in town.states) == pytest.approx(500.0)
    areas = {
        state.damage_state: state.area_m2
        for state in town.states
    }
    assert areas[DamageState.BASIC] == pytest.approx(160.0)
    assert areas[DamageState.COLLAPSED] == pytest.approx(20.0)


def test_top_level_grade_is_worst_town_grade() -> None:
    parameters = ScenarioParameters(
        values={
            **_parameters(era="2020", fractions=(0.5, 0.2, 0.1, 0.1, 0.1)).values,
            **_parameters(era=None, fractions=(0.5, 0.2, 0.1, 0.1, 0.1)).values,
        }
    )
    exposure = build_exposure_dataset(
        snapshot_checksum="b" * 64,
        towns=[
            {
                "town_code": "t1",
                "county_code": "c1",
                "town_name": "测试镇",
                "population_total": 1000.0,
            },
            {
                "town_code": "t2",
                "county_code": "c1",
                "town_name": "测试镇二",
                "population_total": 1000.0,
            },
        ],
        geometries=[
            {
                "town_code": "t1",
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            },
            {
                "town_code": "t2",
                "geometry_wkt": "POLYGON((1 0,2 0,2 1,1 1,1 0))",
            },
        ],
        buildings=[
            {
                "town_code": "t1",
                "structure": "rc_frame",
                "era": "2020",
                "area_m2": 1000.0,
            },
            {
                "town_code": "t2",
                "structure": "rc_frame",
                "era": None,
                "area_m2": 1000.0,
            },
        ],
    )
    result = assess_building_damage(
        exposure,
        (_share(town_code="t1"), _share(town_code="t2")),
        parameters,
    )
    assert result.towns["t1"].quality_grade is LossQualityGrade.L2
    assert result.towns["t2"].quality_grade is LossQualityGrade.L3
    assert result.quality_grade is LossQualityGrade.L3
    assert result.fallback_model_used is True


def test_shared_building_factory_preserves_total_area() -> None:
    result = building_damage_result()
    town = result.towns["t1"]
    assert sum(state.area_m2 for state in town.states) == pytest.approx(
        town.total_area_m2
    )
