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


def _building(
    *,
    town_code: str = "t1",
    quality_grade: LossQualityGrade = LossQualityGrade.L3,
    fallback_model_used: bool = True,
    basic_area: float = 5000.0,
    severe_area: float = 500.0,
    collapsed_area: float = 500.0,
) -> BuildingDamageResult:
    states = (
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.BASIC,
            area_m2=basic_area,
        ),
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.SEVERELY_DAMAGED,
            area_m2=severe_area,
        ),
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=DamageState.COLLAPSED,
            area_m2=collapsed_area,
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
    full_population = sum(item.population for item in by_intensity)
    emergency_shelter_population = min(100.0, affected_population)
    temporary_shelter_population = emergency_shelter_population * 0.5
    return PopulationImpactResult(
        towns={
            town_code: TownPopulationImpact(
                town_code=town_code,
                full_population=full_population,
                affected_population=affected_population,
                emergency_shelter_population=emergency_shelter_population,
                temporary_shelter_population=temporary_shelter_population,
                by_intensity=by_intensity,
            )
        },
        quality_grade=LossQualityGrade.L2,
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
    parameters = _central_parameters()
    result = assess_casualties(
        _building(),
        _population(),
        parameters,
    )
    town = result.towns["t1"]
    severe_fraction = 1000.0 / 6000.0
    expected_deaths = (
        1000.0 * parameters.values["death.a"] * severe_fraction
    )
    assert town.deaths == pytest.approx(expected_deaths)
    assert town.injuries == pytest.approx(
        town.deaths * parameters.values["injury_to_death_ratio.7"]
    )
    assert town.buried == pytest.approx(
        (
            parameters.values["buried.state.severely_damaged"] * 500.0
            + parameters.values["buried.state.collapsed"] * 500.0
        )
        / parameters.values["buried.city_type_factor"]
        * parameters.values["buried.occupancy_factor"]
    )


def test_injuries_and_buried_are_clamped_to_remaining_exposure() -> None:
    parameters = _central_parameters()
    result = assess_casualties(
        _building(
            basic_area=0.0,
            severe_area=6000.0,
            collapsed_area=0.0,
        ),
        _population(affected_population=10.0),
        parameters,
    )
    town = result.towns["t1"]
    assert town.deaths == pytest.approx(10.0)
    assert town.injuries == pytest.approx(0.0)
    assert town.buried == pytest.approx(0.0)


def test_result_uses_worst_input_grade_and_building_fallback() -> None:
    result = assess_casualties(
        _building(
            quality_grade=LossQualityGrade.L3,
            fallback_model_used=True,
        ),
        _population(),
        _central_parameters(),
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
            _central_parameters(),
        )


def test_mismatched_town_coverage_is_unavailable() -> None:
    with pytest.raises(CasualtyAssessmentUnavailable, match="town"):
        assess_casualties(
            _building(town_code="t1"),
            _population(town_code="t2"),
            _central_parameters(),
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
            _central_parameters(),
        )


def test_population_only_intensity_coverage_is_unavailable() -> None:
    with pytest.raises(CasualtyAssessmentUnavailable, match="intensity"):
        assess_casualties(
            _building(),
            _population(
                by_intensity=(
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=6,
                        population=200.0,
                        area_ratio=0.2,
                    ),
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=7,
                        population=800.0,
                        area_ratio=0.8,
                    ),
                )
            ),
            _central_parameters(),
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
    parameters = _central_parameters()
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
    parameters = _central_parameters()
    del parameters.values[key]
    with pytest.raises(CasualtyAssessmentUnavailable, match=key):
        assess_casualties(
            _building(),
            _population(),
            parameters,
        )
