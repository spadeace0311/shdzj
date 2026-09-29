from collections.abc import Mapping
from dataclasses import dataclass
from math import exp, isfinite

from app.loss.buildings import BuildingDamageResult, BuildingDamageStateArea
from app.loss.domain import DamageState, LossQualityGrade, ScenarioParameters
from app.loss.models_registry import canonical_intensity_bin
from app.loss.population import IntensityPopulation, PopulationImpactResult


class CasualtyAssessmentUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TownCasualty:
    town_code: str
    deaths: float
    injuries: float
    buried: float
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class CasualtyResult:
    towns: Mapping[str, TownCasualty]
    quality_grade: LossQualityGrade
    fallback_model_used: bool


_SEVERE_OR_COLLAPSED = frozenset(
    {
        DamageState.SEVERELY_DAMAGED,
        DamageState.COLLAPSED,
    }
)

_GRADE_RANK = (
    LossQualityGrade.L1,
    LossQualityGrade.L2,
    LossQualityGrade.L3,
    LossQualityGrade.L0,
)


def _canonical_intensity(value: int | float | str) -> str:
    try:
        return canonical_intensity_bin(value)
    except (TypeError, ValueError) as exc:
        raise CasualtyAssessmentUnavailable(
            "intensity bin must be a valid I-XII bin"
        ) from exc


def _required_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    try:
        raw_value = parameters.values[key]
    except (KeyError, TypeError) as exc:
        raise CasualtyAssessmentUnavailable(
            f"{key} parameter unavailable"
        ) from exc
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise CasualtyAssessmentUnavailable(
            f"{key} parameter must be numeric"
        ) from exc
    if not isfinite(value):
        raise CasualtyAssessmentUnavailable(
            f"{key} parameter must be finite"
        )
    return value


def _non_negative_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    value = _required_parameter(parameters, key)
    if value < 0.0:
        raise CasualtyAssessmentUnavailable(
            f"{key} parameter must be non-negative"
        )
    return value


def _positive_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    value = _required_parameter(parameters, key)
    if value <= 0.0:
        raise CasualtyAssessmentUnavailable(
            f"{key} parameter must be positive"
        )
    return value


def _non_negative_value(value: object, *, context: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise CasualtyAssessmentUnavailable(
            f"{context} must be numeric"
        ) from exc
    if not isfinite(numeric) or numeric < 0.0:
        raise CasualtyAssessmentUnavailable(
            f"{context} must be finite and non-negative"
        )
    return numeric


def _worst_grade(*grades: LossQualityGrade) -> LossQualityGrade:
    return max(grades, key=_GRADE_RANK.index)


def _death_rate(
    a: float,
    b: float,
    c: float,
    intensity: int,
    severe_fraction: float,
) -> float:
    try:
        exponent = exp(b * (intensity + c))
    except OverflowError:
        exponent = float("inf")
    raw_rate = a * exponent * severe_fraction
    if not isfinite(raw_rate):
        return 1.0 if raw_rate > 0.0 else 0.0
    return min(max(raw_rate, 0.0), 1.0)


def _validate_town_coverage(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
) -> None:
    building_towns = set(buildings.towns)
    population_towns = set(population.towns)
    if not building_towns and not population_towns:
        raise CasualtyAssessmentUnavailable(
            "casualty assessment requires non-empty town coverage"
        )
    if building_towns != population_towns:
        raise CasualtyAssessmentUnavailable(
            "building damage and population impact town coverage do not match"
        )


def assess_casualties(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    parameters: ScenarioParameters,
) -> CasualtyResult:
    _validate_town_coverage(buildings, population)

    death_a = _required_parameter(parameters, "death.a")
    death_b = _required_parameter(parameters, "death.b")
    death_c = _required_parameter(parameters, "death.c")
    buried_coefficients = {
        state: _non_negative_parameter(
            parameters,
            f"buried.state.{state.value}",
        )
        for state in DamageState
    }
    city_type_factor = _positive_parameter(
        parameters,
        "buried.city_type_factor",
    )
    occupancy_factor = _positive_parameter(
        parameters,
        "buried.occupancy_factor",
    )

    towns: dict[str, TownCasualty] = {}
    for town_code in sorted(buildings.towns):
        building_town = buildings.towns[town_code]
        population_town = population.towns[town_code]
        affected_population = _non_negative_value(
            population_town.affected_population,
            context=f"affected population for town {town_code}",
        )

        building_states_by_bin: dict[int, list[BuildingDamageStateArea]] = {}
        for state in building_town.states:
            intensity = int(_canonical_intensity(state.intensity_bin))
            building_states_by_bin.setdefault(intensity, []).append(state)

        population_by_bin: dict[int, IntensityPopulation] = {}
        for item in population_town.by_intensity:
            intensity = int(_canonical_intensity(item.intensity_bin))
            population_by_bin[intensity] = item

        building_bins = set(building_states_by_bin)
        population_bins = set(population_by_bin)
        if not building_bins and not population_bins:
            raise CasualtyAssessmentUnavailable(
                f"town {town_code} has no building or population intensity coverage"
            )
        if building_bins != population_bins:
            building_only = sorted(building_bins - population_bins)
            population_only = sorted(population_bins - building_bins)
            building_only_text = ", ".join(
                str(value) for value in building_only
            ) or "-"
            population_only_text = ", ".join(
                str(value) for value in population_only
            ) or "-"
            raise CasualtyAssessmentUnavailable(
                f"building and population intensity coverage do not match "
                f"for town {town_code}; building only: {building_only_text}; "
                f"population only: {population_only_text}"
            )

        injury_ratios = {
            intensity: _non_negative_parameter(
                parameters,
                f"injury_to_death_ratio.{intensity}",
            )
            for intensity in sorted(building_states_by_bin)
        }

        deaths = 0.0
        injuries = 0.0
        for intensity in sorted(population_by_bin):
            states = building_states_by_bin[intensity]
            denominator = sum(
                _non_negative_value(
                    state.area_m2,
                    context=f"building area for town {town_code}",
                )
                for state in states
            )
            severe_or_collapsed_area = sum(
                _non_negative_value(
                    state.area_m2,
                    context=f"building area for town {town_code}",
                )
                for state in states
                if state.damage_state in _SEVERE_OR_COLLAPSED
            )
            severe_fraction = (
                severe_or_collapsed_area / denominator
                if denominator > 0.0
                else 0.0
            )
            severe_fraction = min(max(severe_fraction, 0.0), 1.0)
            bin_population = _non_negative_value(
                population_by_bin[intensity].population,
                context=f"intensity population for town {town_code}",
            )
            death_rate = _death_rate(
                death_a,
                death_b,
                death_c,
                intensity,
                severe_fraction,
            )
            bin_deaths = bin_population * death_rate
            deaths += bin_deaths
            injuries += bin_deaths * injury_ratios[intensity]

        deaths = min(deaths, affected_population)
        injuries = max(
            0.0,
            min(injuries, affected_population - deaths),
        )

        area_by_state = {
            state: sum(
                _non_negative_value(
                    row.area_m2,
                    context=f"building area for town {town_code}",
                )
                for row in building_town.states
                if row.damage_state is state
            )
            for state in DamageState
        }
        weighted_area = sum(
            buried_coefficients[state] * area_by_state[state]
            for state in DamageState
        )
        raw_buried = weighted_area / city_type_factor * occupancy_factor
        buried = min(
            max(raw_buried, 0.0),
            affected_population - deaths,
        )

        town_grade = _worst_grade(
            building_town.quality_grade,
            population.quality_grade,
        )
        towns[town_code] = TownCasualty(
            town_code=town_code,
            deaths=deaths,
            injuries=injuries,
            buried=buried,
            quality_grade=town_grade,
        )

    return CasualtyResult(
        towns=towns,
        quality_grade=_worst_grade(
            buildings.quality_grade,
            population.quality_grade,
        ),
        fallback_model_used=buildings.fallback_model_used,
    )
