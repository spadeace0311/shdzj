from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil, isfinite

from app.loss.domain import LossQualityGrade, ScenarioParameters
from app.loss.exposure import ExposureDataset
from app.loss.models_registry import canonical_intensity_bin
from app.loss.spatial import TownIntensityShare


class PopulationImpactUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IntensityPopulation:
    town_code: str
    intensity_bin: int
    population: float
    area_ratio: float


@dataclass(frozen=True, slots=True)
class TownPopulationImpact:
    town_code: str
    full_population: float
    affected_population: float
    emergency_shelter_population: float
    temporary_shelter_population: float
    by_intensity: tuple[IntensityPopulation, ...]


@dataclass(frozen=True, slots=True)
class PopulationImpactResult:
    towns: Mapping[str, TownPopulationImpact]
    quality_grade: LossQualityGrade


def _validated_share(share: TownIntensityShare) -> TownIntensityShare:
    if not isfinite(share.area_ratio) or not 0.0 <= share.area_ratio <= 1.0:
        raise ValueError("area_ratio must be between zero and one")
    intensity_bin = int(canonical_intensity_bin(share.intensity_bin))
    return TownIntensityShare(
        town_code=share.town_code,
        intensity_bin=intensity_bin,
        area_ratio=float(share.area_ratio),
        intensity_min=share.intensity_min,
        intensity_max=share.intensity_max,
    )


def _validated_parameter_ratio(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    value = parameters.values[key]
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{key} must be between zero and one")
    return float(value)


def assess_population_impact(
    exposure: ExposureDataset,
    intensity_shares: Sequence[TownIntensityShare],
    parameters: ScenarioParameters,
) -> PopulationImpactResult:
    if not exposure.towns:
        raise PopulationImpactUnavailable(
            "population impact cannot be assessed without exposure"
        )

    shares = tuple(_validated_share(share) for share in intensity_shares)
    exposure_by_town = {town.town_code: town for town in exposure.towns}
    shares_by_town: dict[str, list[TownIntensityShare]] = {}
    for share in shares:
        if share.town_code in exposure_by_town:
            shares_by_town.setdefault(share.town_code, []).append(share)

    affected_min = ceil(
        parameters.values["affected_population_min_intensity"]
    )
    temporary_shelter_ratio = _validated_parameter_ratio(
        parameters,
        "temporary_shelter_ratio",
    )

    towns: dict[str, TownPopulationImpact] = {}
    for town_code in sorted(exposure_by_town):
        town = exposure_by_town[town_code]
        town_shares = sorted(
            shares_by_town.get(town_code, ()),
            key=lambda share: share.intensity_bin,
        )
        if town.population_total > 0.0 and not town_shares:
            raise PopulationImpactUnavailable(
                f"population has no intensity coverage for town {town_code}"
            )

        by_intensity = tuple(
            IntensityPopulation(
                town_code=town_code,
                intensity_bin=share.intensity_bin,
                population=town.population_total * share.area_ratio,
                area_ratio=share.area_ratio,
            )
            for share in town_shares
        )
        affected = tuple(
            item
            for item in by_intensity
            if item.intensity_bin >= affected_min
        )
        affected_population = sum(item.population for item in affected)
        emergency_shelter_population = sum(
            item.population
            * _validated_parameter_ratio(
                parameters,
                f"shelter_ratio.{item.intensity_bin}",
            )
            for item in affected
        )
        temporary_shelter_population = (
            emergency_shelter_population * temporary_shelter_ratio
        )
        if emergency_shelter_population > affected_population:
            raise ValueError(
                "emergency shelter population exceeds affected population"
            )
        if temporary_shelter_population > affected_population:
            raise ValueError(
                "temporary shelter population exceeds affected population"
            )

        towns[town_code] = TownPopulationImpact(
            town_code=town_code,
            full_population=town.population_total,
            affected_population=affected_population,
            emergency_shelter_population=emergency_shelter_population,
            temporary_shelter_population=temporary_shelter_population,
            by_intensity=by_intensity,
        )

    return PopulationImpactResult(
        towns=towns,
        quality_grade=LossQualityGrade.L2,
    )
