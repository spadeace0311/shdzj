from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from app.loss.buildings import BuildingDamageResult
from app.loss.domain import ScenarioParameters


class EconomicLossUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TownEconomicLoss:
    town_code: str
    reconstruction_loss_yuan: float
    contents_loss_yuan: float
    total_loss_yuan: float


@dataclass(frozen=True, slots=True)
class EconomicLossResult:
    towns: Mapping[str, TownEconomicLoss]
    partial_scope: bool
    excluded_scope: tuple[str, ...]


_EXCLUDED_SCOPE = (
    "transport",
    "lifelines",
    "business_interruption",
    "indirect_loss",
    "recovery_duration",
)


def _required_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    try:
        raw_value = parameters.values[key]
    except (KeyError, TypeError) as exc:
        raise EconomicLossUnavailable(
            f"{key} parameter unavailable"
        ) from exc
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise EconomicLossUnavailable(
            f"{key} parameter must be numeric"
        ) from exc
    if not isfinite(value):
        raise EconomicLossUnavailable(
            f"{key} parameter must be finite"
        )
    return value


def _ratio_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    value = _required_parameter(parameters, key)
    if value < 0.0 or value > 1.0:
        raise EconomicLossUnavailable(
            f"{key} parameter must be between 0 and 1"
        )
    return value


def _non_negative_parameter(
    parameters: ScenarioParameters,
    key: str,
) -> float:
    value = _required_parameter(parameters, key)
    if value < 0.0:
        raise EconomicLossUnavailable(
            f"{key} parameter must be non-negative"
        )
    return value


def _non_negative_area(value: object, *, context: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise EconomicLossUnavailable(
            f"{context} must be numeric"
        ) from exc
    if not isfinite(numeric) or numeric < 0.0:
        raise EconomicLossUnavailable(
            f"{context} must be finite and non-negative"
        )
    return numeric


def assess_economic_loss(
    buildings: BuildingDamageResult,
    parameters: ScenarioParameters,
) -> EconomicLossResult:
    towns: dict[str, TownEconomicLoss] = {}
    for town_code in sorted(buildings.towns):
        town = buildings.towns[town_code]
        ordered_states = sorted(
            town.states,
            key=lambda state: (
                state.structure.value,
                str(state.intensity_bin),
                state.damage_state.value,
            ),
        )
        reconstruction_loss = 0.0
        contents_loss = 0.0
        for state in ordered_states:
            area_m2 = _non_negative_area(
                state.area_m2,
                context=f"building area for town {town_code}",
            )
            structure = state.structure.value
            damage_state = state.damage_state.value
            reconstruction_loss += (
                area_m2
                * _ratio_parameter(
                    parameters,
                    f"loss_ratio.{structure}.{damage_state}",
                )
                * _non_negative_parameter(
                    parameters,
                    f"replacement_cost_yuan_m2.{structure}",
                )
            )
            contents_loss += (
                area_m2
                * _ratio_parameter(
                    parameters,
                    f"contents_ratio.{structure}.{damage_state}",
                )
                * _non_negative_parameter(
                    parameters,
                    f"contents_value_yuan_m2.{structure}",
                )
            )
        towns[town_code] = TownEconomicLoss(
            town_code=town_code,
            reconstruction_loss_yuan=reconstruction_loss,
            contents_loss_yuan=contents_loss,
            total_loss_yuan=reconstruction_loss + contents_loss,
        )

    return EconomicLossResult(
        towns=towns,
        partial_scope=True,
        excluded_scope=_EXCLUDED_SCOPE,
    )
