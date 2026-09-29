from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil, isfinite

from app.loss.buildings import BuildingDamageResult
from app.loss.casualties import CasualtyResult
from app.loss.domain import ResourceKind, ScenarioParameters
from app.loss.population import PopulationImpactResult


class ResourceDemandUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceDemandInputs:
    affected_population: float
    emergency_shelter_population: float
    deaths: float
    injuries: float
    buried: float


@dataclass(frozen=True, slots=True)
class ResourceDemandValue:
    quantity: int | None
    status: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceDemandResult:
    inputs: ResourceDemandInputs
    values: Mapping[ResourceKind, ResourceDemandValue]


RESOURCE_COEFFICIENTS = {
    ResourceKind.RESCUE_TEAM: (
        "rescue_team.base",
        "rescue_team.rescuer_per_missing",
    ),
    ResourceKind.MEDICAL_TEAM: (
        "medical_team.base",
        "medical_team.team_per_injury",
    ),
    ResourceKind.EPIDEMIC_TEAM: (
        "epidemic_team.base",
        "epidemic_team.team_per_shelter_10000",
    ),
    ResourceKind.TENT: (
        "tent.base",
        "tent.per_shelter_population",
    ),
    ResourceKind.DRINKING_WATER: (
        "drinking_water.base",
        "drinking_water.per_affected_population",
    ),
    ResourceKind.TOILET: (
        "toilet.base",
        "toilet.per_shelter_population",
    ),
    ResourceKind.CLOTHING: (
        "clothing.base",
        "clothing.per_affected_population",
    ),
    ResourceKind.QUILT: (
        "quilt.base",
        "quilt.per_shelter_population",
    ),
    ResourceKind.FOOD: (
        "food.base",
        "food.per_affected_population",
    ),
    ResourceKind.BLANKET: (
        "blanket.base",
        "blanket.per_affected_population",
    ),
    ResourceKind.STRETCHER: (
        "stretcher.base",
        "stretcher.per_injury",
    ),
    ResourceKind.SICKBED: (
        "sickbed.base",
        "sickbed.per_injury",
    ),
}


def _validate_town_coverage(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
) -> None:
    building_towns = set(buildings.towns)
    population_towns = set(population.towns)
    casualty_towns = set(casualties.towns)
    if not building_towns or not population_towns or not casualty_towns:
        raise ResourceDemandUnavailable(
            "resource demand requires non-empty town coverage"
        )
    if building_towns != population_towns or building_towns != casualty_towns:
        raise ResourceDemandUnavailable(
            "building damage, population impact, and casualty town coverage "
            "do not match"
        )


def _non_negative_value(value: object, *, context: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ResourceDemandUnavailable(
            f"{context} must be numeric"
        ) from exc
    if not isfinite(numeric) or numeric < 0.0:
        raise ResourceDemandUnavailable(
            f"{context} must be finite and non-negative"
        )
    return numeric


def _aggregate_town_values(
    towns: Mapping[str, object],
    attribute: str,
    *,
    context: str,
) -> float:
    total = 0.0
    for town_code in sorted(towns):
        value = getattr(towns[town_code], attribute)
        total += _non_negative_value(
            value,
            context=f"{context} for town {town_code}",
        )
    return total


def _resource_coefficients(
    parameters: ScenarioParameters,
    keys: tuple[str, ...],
) -> tuple[dict[str, float] | None, str | None]:
    missing_key = next(
        (key for key in keys if key not in parameters.values),
        None,
    )
    if missing_key is not None:
        return None, f"missing_parameter:{missing_key}"

    coefficients: dict[str, float] = {}
    for key in keys:
        raw_value = parameters.values[key]
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None, f"invalid_parameter:{key}"
        if not isfinite(value) or value < 0.0:
            return None, f"invalid_parameter:{key}"
        coefficients[key] = value
    return coefficients, None


def _raw_demand(
    kind: ResourceKind,
    coefficients: dict[str, float],
    inputs: ResourceDemandInputs,
) -> float:
    base = coefficients[f"{kind.value}.base"]
    if kind is ResourceKind.RESCUE_TEAM:
        return (
            base
            + coefficients["rescue_team.rescuer_per_missing"] * inputs.buried
        )
    if kind is ResourceKind.MEDICAL_TEAM:
        return (
            base
            + coefficients["medical_team.team_per_injury"] * inputs.injuries
        )
    if kind is ResourceKind.EPIDEMIC_TEAM:
        return (
            base
            + coefficients["epidemic_team.team_per_shelter_10000"]
            * inputs.emergency_shelter_population
            / 10000.0
        )
    if kind is ResourceKind.TENT:
        return (
            base
            + coefficients["tent.per_shelter_population"]
            * inputs.emergency_shelter_population
        )
    if kind is ResourceKind.DRINKING_WATER:
        return (
            base
            + coefficients["drinking_water.per_affected_population"]
            * inputs.affected_population
        )
    if kind is ResourceKind.TOILET:
        return (
            base
            + coefficients["toilet.per_shelter_population"]
            * inputs.emergency_shelter_population
        )
    if kind is ResourceKind.CLOTHING:
        return (
            base
            + coefficients["clothing.per_affected_population"]
            * inputs.affected_population
        )
    if kind is ResourceKind.QUILT:
        return (
            base
            + coefficients["quilt.per_shelter_population"]
            * inputs.emergency_shelter_population
        )
    if kind is ResourceKind.FOOD:
        return (
            base
            + coefficients["food.per_affected_population"]
            * inputs.affected_population
        )
    if kind is ResourceKind.BLANKET:
        return (
            base
            + coefficients["blanket.per_affected_population"]
            * inputs.affected_population
        )
    if kind is ResourceKind.STRETCHER:
        return base + coefficients["stretcher.per_injury"] * inputs.injuries
    return base + coefficients["sickbed.per_injury"] * inputs.injuries


def assess_resource_demand(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
    parameters: ScenarioParameters,
) -> ResourceDemandResult:
    _validate_town_coverage(buildings, population, casualties)

    inputs = ResourceDemandInputs(
        affected_population=_aggregate_town_values(
            population.towns,
            "affected_population",
            context="affected population",
        ),
        emergency_shelter_population=_aggregate_town_values(
            population.towns,
            "emergency_shelter_population",
            context="emergency shelter population",
        ),
        deaths=_aggregate_town_values(
            casualties.towns,
            "deaths",
            context="deaths",
        ),
        injuries=_aggregate_town_values(
            casualties.towns,
            "injuries",
            context="injuries",
        ),
        buried=_aggregate_town_values(
            casualties.towns,
            "buried",
            context="buried",
        ),
    )

    values: dict[ResourceKind, ResourceDemandValue] = {}
    for kind in ResourceKind:
        coefficients, reason = _resource_coefficients(
            parameters,
            RESOURCE_COEFFICIENTS[kind],
        )
        if coefficients is None:
            values[kind] = ResourceDemandValue(
                quantity=None,
                status="unavailable",
                reason=reason,
            )
            continue

        raw = _raw_demand(kind, coefficients, inputs)
        values[kind] = ResourceDemandValue(
            quantity=ceil(max(raw, 0.0)),
            status="available",
            reason=None,
        )

    return ResourceDemandResult(inputs=inputs, values=values)
