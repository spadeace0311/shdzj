from pathlib import Path

import pytest

from app.loss.buildings import (
    BuildingDamageResult,
    BuildingDamageStateArea,
    TownBuildingDamage,
)
from app.loss.casualties import (
    CasualtyAssessmentUnavailable,
    assess_casualties,
)
from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossModelType,
    LossQualityGrade,
    LossValueType,
    ScenarioParameters,
)
from app.loss.models_registry import load_parameter_set
from app.loss.population import (
    IntensityPopulation,
    PopulationImpactResult,
    TownPopulationImpact,
)
from tests.loss_factories import (
    building_damage_result as _building_result,
    population_impact_result as _population_result,
)


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def _central_parameters() -> ScenarioParameters:
    return load_parameter_set(PARAMETERS).models[
        LossModelType.CASUALTIES
    ].scenarios[LossValueType.CENTRAL]


def _parameters(values: dict[str, float]) -> ScenarioParameters:
    return ScenarioParameters(values=values)


def _building(
    *,
    town_code: str = "t1",
    quality_grade: LossQualityGrade = LossQualityGrade.L3,
    fallback_model_used: bool = True,
) -> BuildingDamageResult:
    states = (
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.BASIC,
            area_m2=5000.0,
        ),
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.SEVERELY_DAMAGED,
            area_m2=500.0,
        ),
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.COLLAPSED,
            area_m2=500.0,
        ),
    )
    return BuildingDamageResult(
        towns={
            town_code: TownBuildingDamage(
                town_code=town_code,
                total_area_m2=sum(state.area_m2 for state in states),
                states=states,
                quality_grade=quality_grade,
            )
        },
        quality_grade=quality_grade,
        fallback_model_used=fallback_model_used,
    )


def _population(
    *,
    town_code: str = "t1",
    affected_population: float = 1000.0,
    by_intensity: tuple[IntensityPopulation, ...] | None = None,
) -> PopulationImpactResult:
    if by_intensity is None:
        by_intensity = (
            IntensityPopulation(
                town_code=town_code,
                intensity_bin=7,
                population=1000.0,
                area_ratio=1.0,
            ),
        )
    return PopulationImpactResult(
        towns={
            town_code: TownPopulationImpact(
                town_code=town_code,
                full_population=affected_population,
                affected_population=affected_population,
                emergency_shelter_population=100.0,
                temporary_shelter_population=50.0,
                by_intensity=by_intensity,
            )
        },
        quality_grade=LossQualityGrade.L2,
    )


def _exact_parameters() -> ScenarioParameters:
    return _parameters(
        {
            "death.a": 0.02,
            "death.b": 0.0,
            "death.c": 0.0,
            "injury_to_death_ratio.7": 2.0,
            "buried.state.basic": 0.0,
            "buried.state.slightly_damaged": 0.0,
            "buried.state.moderately_damaged": 0.0,
            "buried.state.severely_damaged": 0.01,
            "buried.state.collapsed": 0.02,
            "buried.city_type_factor": 2.0,
            "buried.occupancy_factor": 3.0,
        }
    )


def test_casualties_are_non_negative_and_below_exposure() -> None:
    parameters = _central_parameters()
    buildings = _building_result()
    population = _population_result()
    result = assess_casualties(buildings, population, parameters)
    town = result.towns["t1"]
    assert town.deaths >= 0
    assert town.injuries >= town.deaths
    assert town.buried >= 0
    assert town.deaths + town.injuries <= population.towns["t1"].affected_population


def test_death_rate_is_monotonic_when_damage_fraction_increases() -> None:
    parameters = _central_parameters()
    low = assess_casualties(
        _building_result(severe_fraction=0.05),
        _population_result(),
        parameters,
    )
    high = assess_casualties(
        _building_result(severe_fraction=0.20),
        _population_result(),
        parameters,
    )
    assert high.towns["t1"].deaths >= low.towns["t1"].deaths


def test_death_injury_and_buried_arithmetic() -> None:
    result = assess_casualties(
        _building(),
        _population(),
        _exact_parameters(),
    )
    town = result.towns["t1"]
    severe_fraction = 1000.0 / 6000.0
    assert town.deaths == pytest.approx(1000.0 * 0.02 * severe_fraction)
    assert town.injuries == pytest.approx(town.deaths * 2.0)
    assert town.buried == pytest.approx(
        (0.01 * 500.0 + 0.02 * 500.0) / 2.0 * 3.0
    )


def test_injuries_and_buried_are_clamped_to_remaining_exposure() -> None:
    parameters = _exact_parameters()
    parameters.values["death.a"] = 6.0
    result = assess_casualties(
        _building(),
        _population(affected_population=100.0),
        parameters,
    )
    town = result.towns["t1"]
    assert town.deaths == pytest.approx(100.0)
    assert town.injuries == pytest.approx(0.0)
    assert town.buried == pytest.approx(0.0)


def test_result_uses_worst_input_grade_and_building_fallback() -> None:
    result = assess_casualties(
        _building(
            quality_grade=LossQualityGrade.L3,
            fallback_model_used=True,
        ),
        _population(),
        _exact_parameters(),
    )
    assert result.quality_grade is LossQualityGrade.L3
    assert result.towns["t1"].quality_grade is LossQualityGrade.L3
    assert result.fallback_model_used is True


def test_reference_uncalibrated_result_is_not_l1() -> None:
    result = assess_casualties(
        _building_result(),
        _population_result(),
        _central_parameters(),
    )
    assert result.quality_grade is not LossQualityGrade.L1


def test_empty_town_coverage_is_unavailable() -> None:
    with pytest.raises(CasualtyAssessmentUnavailable, match="town"):
        assess_casualties(
            BuildingDamageResult(
                towns={},
                quality_grade=LossQualityGrade.L2,
                fallback_model_used=False,
            ),
            PopulationImpactResult(
                towns={},
                quality_grade=LossQualityGrade.L2,
            ),
            _exact_parameters(),
        )


def test_mismatched_town_coverage_is_unavailable() -> None:
    with pytest.raises(CasualtyAssessmentUnavailable, match="town"):
        assess_casualties(
            _building(town_code="t1"),
            _population(town_code="t2"),
            _exact_parameters(),
        )


def test_missing_intensity_population_coverage_is_unavailable() -> None:
    with pytest.raises(CasualtyAssessmentUnavailable, match="intensity"):
        assess_casualties(
            _building(),
            _population(
                by_intensity=(
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=6,
                        population=1000.0,
                        area_ratio=1.0,
                    ),
                )
            ),
            _exact_parameters(),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("death.a", float("nan")),
        ("death.b", float("inf")),
        ("death.c", float("-inf")),
        ("injury_to_death_ratio.7", float("nan")),
        ("buried.state.collapsed", float("inf")),
        ("buried.city_type_factor", 0.0),
        ("buried.occupancy_factor", float("nan")),
    ],
)
def test_non_finite_or_zero_required_parameter_is_unavailable(
    key: str,
    value: float,
) -> None:
    parameters = _exact_parameters()
    parameters.values[key] = value
    with pytest.raises(CasualtyAssessmentUnavailable, match=key):
        assess_casualties(
            _building(),
            _population(),
            parameters,
        )


@pytest.mark.parametrize(
    "key",
    [
        "death.a",
        "death.b",
        "death.c",
        "injury_to_death_ratio.7",
        "buried.state.basic",
        "buried.city_type_factor",
        "buried.occupancy_factor",
    ],
)
def test_missing_required_parameter_is_unavailable(key: str) -> None:
    parameters = _exact_parameters()
    del parameters.values[key]
    with pytest.raises(CasualtyAssessmentUnavailable, match=key):
        assess_casualties(
            _building(),
            _population(),
            parameters,
        )
