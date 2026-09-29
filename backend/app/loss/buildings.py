from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossQualityGrade,
    ScenarioParameters,
)
from app.loss.exposure import BuildingExposure, ExposureDataset
from app.loss.models_registry import canonical_intensity_bin
from app.loss.spatial import TownIntensityShare


@dataclass(frozen=True, slots=True)
class BuildingDamageStateArea:
    structure: BuildingStructure
    intensity_bin: int
    damage_state: DamageState
    area_m2: float


@dataclass(frozen=True, slots=True)
class TownBuildingDamage:
    town_code: str
    total_area_m2: float
    states: tuple[BuildingDamageStateArea, ...]
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class BuildingDamageResult:
    towns: Mapping[str, TownBuildingDamage]
    quality_grade: LossQualityGrade
    fallback_model_used: bool


def _vulnerability_key(
    *,
    structure: BuildingStructure,
    era: str | None,
    intensity_bin: int,
    damage_state: DamageState,
) -> str:
    bin_value = canonical_intensity_bin(intensity_bin)
    segments = ("vulnerability", structure.value)
    if era is not None:
        segments = (*segments, era)
    return ".".join((*segments, bin_value, damage_state.value))


def _grade_rank(grade: LossQualityGrade) -> int:
    return (
        LossQualityGrade.L1,
        LossQualityGrade.L2,
        LossQualityGrade.L3,
        LossQualityGrade.L0,
    ).index(grade)


def _damage_state_areas(
    *,
    building: BuildingExposure,
    share: TownIntensityShare,
    parameters: ScenarioParameters,
) -> tuple[BuildingDamageStateArea, ...]:
    available_area = building.area_m2 * share.area_ratio
    raw_areas: dict[DamageState, float] = {}
    for damage_state in DamageState:
        key = _vulnerability_key(
            structure=building.structure,
            era=building.era,
            intensity_bin=share.intensity_bin,
            damage_state=damage_state,
        )
        coefficient = parameters.values[key]
        raw_areas[damage_state] = min(
            building.area_m2 * share.area_ratio * coefficient,
            available_area,
        )

    raw_total = sum(raw_areas.values())
    if raw_total > available_area and available_area > 0.0:
        scale = available_area / raw_total
        scaled_areas = {
            damage_state: area_m2 * scale
            for damage_state, area_m2 in raw_areas.items()
        }
    else:
        scaled_areas = raw_areas

    return tuple(
        BuildingDamageStateArea(
            structure=building.structure,
            intensity_bin=share.intensity_bin,
            damage_state=damage_state,
            area_m2=scaled_areas[damage_state],
        )
        for damage_state in DamageState
    )


def assess_building_damage(
    exposure: ExposureDataset,
    intensity_shares: Sequence[TownIntensityShare],
    parameters: ScenarioParameters,
) -> BuildingDamageResult:
    exposure_by_town = {town.town_code: town for town in exposure.towns}
    shares_by_town: dict[str, list[TownIntensityShare]] = {}
    for share in intensity_shares:
        if share.town_code in exposure_by_town:
            shares_by_town.setdefault(share.town_code, []).append(share)

    towns: dict[str, TownBuildingDamage] = {}
    fallback_model_used = False
    for town_code in sorted(exposure_by_town):
        town = exposure_by_town[town_code]
        states: list[BuildingDamageStateArea] = []
        town_fallback_model_used = False
        for building in town.buildings:
            for share in shares_by_town.get(town_code, ()):
                states.extend(
                    _damage_state_areas(
                        building=building,
                        share=share,
                        parameters=parameters,
                    )
                )
                if building.era is None:
                    town_fallback_model_used = True

        total_area_m2 = sum(state.area_m2 for state in states)
        quality_grade = (
            LossQualityGrade.L3
            if town_fallback_model_used
            else LossQualityGrade.L2
        )
        towns[town_code] = TownBuildingDamage(
            town_code=town_code,
            total_area_m2=total_area_m2,
            states=tuple(states),
            quality_grade=quality_grade,
        )
        fallback_model_used = fallback_model_used or town_fallback_model_used

    if not towns:
        result_grade = LossQualityGrade.L2
    else:
        result_grade = max(
            (town.quality_grade for town in towns.values()),
            key=_grade_rank,
        )
    return BuildingDamageResult(
        towns=towns,
        quality_grade=result_grade,
        fallback_model_used=fallback_model_used,
    )
