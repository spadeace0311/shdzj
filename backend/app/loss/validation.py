from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isclose, isfinite

from app.loss.buildings import BuildingDamageResult
from app.loss.casualties import CasualtyResult
from app.loss.domain import LossProductType, LossQualityGrade, ResourceKind
from app.loss.economic import EconomicLossResult
from app.loss.population import PopulationImpactResult
from app.loss.resources import ResourceDemandResult


class ValidationInputUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool
    context: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    blocking_issues: tuple[ValidationIssue, ...]
    review_issues: tuple[ValidationIssue, ...]
    needs_review: bool
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class GridResidual:
    area_scope: str
    area_code: str
    residual: float


@dataclass(frozen=True, slots=True)
class ValidationContext:
    region_profile_version: str
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    data_asset_snapshot_fingerprint: str | None
    product_snapshot_fingerprints: Mapping[LossProductType, str | None]


@dataclass(frozen=True, slots=True)
class LossValidationSnapshot:
    buildings: BuildingDamageResult
    population: PopulationImpactResult
    casualties: CasualtyResult
    economic: EconomicLossResult
    resources: ResourceDemandResult
    coverage_ratio: float
    grid_residuals: tuple[GridResidual, ...]
    product_snapshot_fingerprints: Mapping[LossProductType, str | None]


_GRADE_RANK = (
    LossQualityGrade.L1,
    LossQualityGrade.L2,
    LossQualityGrade.L3,
    LossQualityGrade.L0,
)

_REQUIRED_PRODUCT_TYPES = frozenset(
    {
        LossProductType.BUILDING_DAMAGE,
        LossProductType.POPULATION_IMPACT,
        LossProductType.CASUALTIES,
        LossProductType.ECONOMIC_LOSS,
        LossProductType.RESOURCE_DEMAND,
    }
)

_FLOAT_TOLERANCE = 1e-6


def _issue(
    code: str,
    message: str,
    *,
    blocking: bool,
    context: Mapping[str, object] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        blocking=blocking,
        context=dict(context or {}),
    )


def _context_value(value: object) -> object:
    if isinstance(value, str):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if isfinite(numeric):
        return numeric
    return str(value)


def _finite_non_negative(value: object) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(numeric) and numeric >= 0.0


def _finite_number(value: object) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(numeric)


def _validate_context(context: ValidationContext) -> None:
    if not isinstance(context, ValidationContext):
        raise ValidationInputUnavailable("validation context is required")
    if (
        not _finite_number(context.minimum_town_coverage_ratio)
        or not 0.0 <= float(context.minimum_town_coverage_ratio) <= 1.0
    ):
        raise ValidationInputUnavailable(
            "minimum_town_coverage_ratio must be finite and between zero and one"
        )
    if (
        not _finite_number(context.grid_residual_review_threshold)
        or float(context.grid_residual_review_threshold) < 0.0
    ):
        raise ValidationInputUnavailable(
            "grid_residual_review_threshold must be finite and non-negative"
        )
    if context.product_snapshot_fingerprints is None:
        raise ValidationInputUnavailable(
            "product_snapshot_fingerprints is required"
        )


def _sorted_building_states(town) -> tuple[object, ...]:
    return tuple(
        sorted(
            town.states,
            key=lambda state: (
                state.structure.value,
                str(state.intensity_bin),
                state.damage_state.value,
            ),
        )
    )


def _validate_missing_core_exposure(
    buildings: BuildingDamageResult,
    blocking: list[ValidationIssue],
) -> None:
    if not buildings.towns:
        blocking.append(
            _issue(
                "missing_core_exposure",
                "building damage result has no town coverage",
                blocking=True,
                context={
                    "town_code": None,
                    "town_count": 0,
                    "state_count": 0,
                    "total_area_m2": None,
                },
            )
        )
        return

    for town_code in sorted(buildings.towns):
        town = buildings.towns[town_code]
        state_count = len(town.states)
        total_area_m2 = town.total_area_m2
        if state_count == 0:
            reason = "no_building_states"
        elif not _finite_number(total_area_m2) or float(total_area_m2) <= 0.0:
            reason = "non_positive_total_area"
        else:
            continue
        blocking.append(
            _issue(
                "missing_core_exposure",
                f"building damage result for town {town_code} has no usable "
                "core building exposure",
                blocking=True,
                context={
                    "town_code": town_code,
                    "town_count": len(buildings.towns),
                    "state_count": state_count,
                    "total_area_m2": _context_value(total_area_m2),
                    "reason": reason,
                },
            )
        )


def _validate_negative_metrics(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
    economic: EconomicLossResult,
    resources: ResourceDemandResult,
    blocking: list[ValidationIssue],
) -> None:
    for town_code in sorted(buildings.towns):
        town = buildings.towns[town_code]
        for field in ("total_area_m2",):
            value = getattr(town, field)
            if not _finite_non_negative(value):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"building {field} for town {town_code} must be finite "
                        "and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": field,
                            "value": _context_value(value),
                        },
                    )
                )
        for index, state in enumerate(_sorted_building_states(town)):
            value = state.area_m2
            if not _finite_non_negative(value):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"building damage area for town {town_code} state "
                        f"{index} must be finite and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": "building_state_area_m2",
                            "state_index": index,
                            "value": _context_value(value),
                        },
                    )
                )

    for town_code in sorted(population.towns):
        town = population.towns[town_code]
        for field in (
            "full_population",
            "affected_population",
            "emergency_shelter_population",
            "temporary_shelter_population",
        ):
            value = getattr(town, field)
            if not _finite_non_negative(value):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"population {field} for town {town_code} must be finite "
                        "and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": field,
                            "value": _context_value(value),
                        },
                    )
                )
        for index, item in enumerate(town.by_intensity):
            if not _finite_non_negative(item.population):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"intensity population for town {town_code} item "
                        f"{index} must be finite and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": "by_intensity.population",
                            "intensity_bin": item.intensity_bin,
                            "value": _context_value(item.population),
                        },
                    )
                )

    for town_code in sorted(casualties.towns):
        town = casualties.towns[town_code]
        for field in ("deaths", "injuries", "buried"):
            value = getattr(town, field)
            if not _finite_non_negative(value):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"casualty {field} for town {town_code} must be finite "
                        "and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": field,
                            "value": _context_value(value),
                        },
                    )
                )

    for town_code in sorted(economic.towns):
        town = economic.towns[town_code]
        for field in (
            "reconstruction_loss_yuan",
            "contents_loss_yuan",
            "total_loss_yuan",
        ):
            value = getattr(town, field)
            if not _finite_non_negative(value):
                blocking.append(
                    _issue(
                        "negative_metric",
                        f"economic {field} for town {town_code} must be finite "
                        "and non-negative",
                        blocking=True,
                        context={
                            "town_code": town_code,
                            "field": field,
                            "value": _context_value(value),
                        },
                    )
                )

    for field in (
        "affected_population",
        "emergency_shelter_population",
        "deaths",
        "injuries",
        "buried",
    ):
        value = getattr(resources.inputs, field)
        if not _finite_non_negative(value):
            blocking.append(
                _issue(
                    "negative_metric",
                    f"resource input {field} must be finite and non-negative",
                    blocking=True,
                    context={
                        "town_code": None,
                        "field": f"resources.inputs.{field}",
                        "value": _context_value(value),
                    },
                )
            )

    for kind in ResourceKind:
        value = resources.values.get(kind)
        if (
            value is not None
            and value.quantity is not None
            and not _finite_non_negative(value.quantity)
        ):
            blocking.append(
                _issue(
                    "negative_metric",
                    f"resource quantity for {kind.value} must be finite and "
                    "non-negative",
                    blocking=True,
                    context={
                        "resource_kind": kind.value,
                        "value": _context_value(value.quantity),
                    },
                )
            )


def _validate_building_stock(
    buildings: BuildingDamageResult,
    blocking: list[ValidationIssue],
) -> None:
    for town_code in sorted(buildings.towns):
        town = buildings.towns[town_code]
        if not _finite_number(town.total_area_m2):
            continue
        state_total = sum(
            float(state.area_m2)
            for state in town.states
            if _finite_number(state.area_m2)
        )
        if not isclose(
            state_total,
            float(town.total_area_m2),
            rel_tol=1e-9,
            abs_tol=_FLOAT_TOLERANCE,
        ) and state_total > float(town.total_area_m2):
            blocking.append(
                _issue(
                    "building_damage_exceeds_stock",
                    f"building damage area for town {town_code} exceeds total "
                    "building stock",
                    blocking=True,
                    context={
                        "town_code": town_code,
                        "stock_area_m2": _context_value(town.total_area_m2),
                        "damage_area_m2": _context_value(state_total),
                    },
                )
            )


def _validate_shelter(
    population: PopulationImpactResult,
    resources: ResourceDemandResult,
    blocking: list[ValidationIssue],
) -> None:
    for town_code in sorted(population.towns):
        town = population.towns[town_code]
        shelter = town.emergency_shelter_population
        affected = town.affected_population
        if (
            _finite_number(shelter)
            and _finite_number(affected)
            and float(shelter) > float(affected) + _FLOAT_TOLERANCE
        ):
            blocking.append(
                _issue(
                    "shelter_exceeds_affected",
                    f"emergency shelter population for town {town_code} exceeds "
                    "affected population",
                    blocking=True,
                    context={
                        "town_code": town_code,
                        "shelter_population": _context_value(shelter),
                        "affected_population": _context_value(affected),
                    },
                )
            )

    shelter = resources.inputs.emergency_shelter_population
    affected = resources.inputs.affected_population
    if (
        _finite_number(shelter)
        and _finite_number(affected)
        and float(shelter) > float(affected) + _FLOAT_TOLERANCE
    ):
        blocking.append(
            _issue(
                "shelter_exceeds_affected",
                "aggregate emergency shelter population exceeds affected "
                "population",
                blocking=True,
                context={
                    "town_code": None,
                    "shelter_population": _context_value(shelter),
                    "affected_population": _context_value(affected),
                },
            )
        )


def _sum_population_towns(population: PopulationImpactResult) -> dict[str, float]:
    totals: dict[str, float] = {
        "affected_population": 0.0,
        "emergency_shelter_population": 0.0,
    }
    for town in population.towns.values():
        if _finite_number(town.affected_population):
            totals["affected_population"] += float(town.affected_population)
        if _finite_number(town.emergency_shelter_population):
            totals["emergency_shelter_population"] += float(
                town.emergency_shelter_population
            )
    return totals


def _sum_casualty_towns(casualties: CasualtyResult) -> dict[str, float]:
    totals: dict[str, float] = {"deaths": 0.0, "injuries": 0.0, "buried": 0.0}
    for town in casualties.towns.values():
        for field in totals:
            value = getattr(town, field)
            if _finite_number(value):
                totals[field] += float(value)
    return totals


def _validate_sum_scope(
    population: PopulationImpactResult,
    casualties: CasualtyResult,
    resources: ResourceDemandResult,
    blocking: list[ValidationIssue],
) -> None:
    expected = {
        **_sum_population_towns(population),
        **_sum_casualty_towns(casualties),
    }
    for field, expected_value in expected.items():
        actual = getattr(resources.inputs, field)
        if not _finite_number(actual) or not isclose(
            float(actual),
            expected_value,
            rel_tol=1e-9,
            abs_tol=_FLOAT_TOLERANCE,
        ):
            blocking.append(
                _issue(
                    "sum_scope_mismatch",
                    f"resource input {field} does not match the town-scope sum",
                    blocking=True,
                    context={
                        "field": field,
                        "expected": _context_value(expected_value),
                        "actual": _context_value(actual),
                    },
                )
            )


def _result_town_codes(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
    economic: EconomicLossResult,
) -> set[str]:
    return (
        set(buildings.towns)
        | set(population.towns)
        | set(casualties.towns)
        | set(economic.towns)
    )


def _validate_grid_structure(
    coverage_ratio: object,
    grid_residuals: Sequence[GridResidual],
    required_town_codes: set[str],
    blocking: list[ValidationIssue],
) -> bool:
    coverage_valid = (
        _finite_number(coverage_ratio)
        and 0.0 <= float(coverage_ratio) <= 1.0
    )
    if not coverage_valid:
        blocking.append(
            _issue(
                "grid_total_mismatch",
                "coverage ratio must be finite and between zero and one",
                blocking=True,
                context={
                    "reason": "coverage_out_of_range",
                    "coverage_ratio": _context_value(coverage_ratio),
                },
            )
        )

    seen: set[tuple[str, str]] = set()
    residual_town_codes: set[str] = set()
    for residual in grid_residuals:
        if not isinstance(residual, GridResidual):
            raise ValidationInputUnavailable("grid residual must be a GridResidual")
        scope = residual.area_scope
        code = residual.area_code
        value = residual.residual
        blank_scope = not isinstance(scope, str) or not scope.strip()
        blank_code = not isinstance(code, str) or not code.strip()
        if blank_scope or blank_code:
            blocking.append(
                _issue(
                    "grid_total_mismatch",
                    "grid residual scope and area code are required",
                    blocking=True,
                    context={
                        "reason": "blank_residual_scope_or_code",
                        "area_scope": _context_value(scope),
                        "area_code": _context_value(code),
                    },
                )
            )
        if not _finite_number(value):
            blocking.append(
                _issue(
                    "grid_total_mismatch",
                    "grid residual value must be finite",
                    blocking=True,
                    context={
                        "reason": "non_finite_residual",
                        "area_scope": _context_value(scope),
                        "area_code": _context_value(code),
                        "residual": _context_value(value),
                    },
                )
            )
        if isinstance(scope, str) and isinstance(code, str):
            key = (scope, code)
            if key in seen:
                blocking.append(
                    _issue(
                        "grid_total_mismatch",
                        "grid residual contains duplicate scope and area code",
                        blocking=True,
                        context={
                            "reason": "duplicate_residual_scope",
                            "area_scope": scope,
                            "area_code": code,
                        },
                    )
                )
            else:
                seen.add(key)
                residual_town_codes.add(code)

    for town_code in sorted(required_town_codes - residual_town_codes):
        blocking.append(
            _issue(
                "grid_total_mismatch",
                f"grid residual is missing for required town {town_code}",
                blocking=True,
                context={
                    "reason": "missing_required_residual",
                    "town_code": town_code,
                },
            )
        )

    return coverage_valid


def _validate_grid_review(
    grid_residuals: Sequence[GridResidual],
    threshold: float,
    review: list[ValidationIssue],
) -> None:
    for residual in grid_residuals:
        if not isinstance(residual, GridResidual):
            raise ValidationInputUnavailable("grid residual must be a GridResidual")
        value = residual.residual
        if not _finite_number(value):
            continue
        if abs(float(value)) > float(threshold):
            review.append(
                _issue(
                    "grid_residual_threshold",
                    f"grid residual for {residual.area_scope} {residual.area_code} "
                    "exceeds review threshold",
                    blocking=False,
                    context={
                        "area_scope": residual.area_scope,
                        "area_code": residual.area_code,
                        "threshold": _context_value(threshold),
                        "actual": _context_value(value),
                    },
                )
            )


def _validate_stale_data_asset(
    context: ValidationContext,
    review: list[ValidationIssue],
) -> None:
    fingerprints = context.product_snapshot_fingerprints
    expected = _REQUIRED_PRODUCT_TYPES
    actual_types = set(fingerprints)
    mismatched = tuple(
        sorted(
            product_type.value
            for product_type in expected
            if fingerprints.get(product_type)
            != context.data_asset_snapshot_fingerprint
        )
    )
    missing = tuple(
        sorted(product_type.value for product_type in expected - actual_types)
    )
    if (
        context.data_asset_snapshot_fingerprint is None
        or actual_types != expected
        or mismatched
    ):
        review.append(
            _issue(
                "stale_data_asset",
                "loss products do not all use the locked data-asset snapshot",
                blocking=False,
                context={
                    "data_asset_snapshot_fingerprint": (
                        context.data_asset_snapshot_fingerprint
                    ),
                    "missing_product_types": missing,
                    "mismatched_product_types": mismatched,
                },
            )
        )


def _validate_review_flags(
    buildings: BuildingDamageResult,
    casualties: CasualtyResult,
    economic: EconomicLossResult,
    review: list[ValidationIssue],
) -> None:
    if buildings.fallback_model_used or casualties.fallback_model_used:
        fallback_products = []
        if buildings.fallback_model_used:
            fallback_products.append(LossProductType.BUILDING_DAMAGE.value)
        if casualties.fallback_model_used:
            fallback_products.append(LossProductType.CASUALTIES.value)
        review.append(
            _issue(
                "fallback_model_used",
                "one or more loss products used a fallback model",
                blocking=False,
                context={"product_types": tuple(fallback_products)},
            )
        )

    if economic.partial_scope:
        review.append(
            _issue(
                "partial_scope",
                "economic loss result covers only part of the requested scope",
                blocking=False,
                context={"excluded_scope": tuple(economic.excluded_scope)},
            )
        )


def _clean_quality_grade(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
) -> LossQualityGrade:
    worst = max(
        (
            buildings.quality_grade,
            population.quality_grade,
            casualties.quality_grade,
        ),
        key=_GRADE_RANK.index,
    )
    if worst is LossQualityGrade.L1:
        return LossQualityGrade.L2
    return worst


def _build_result(
    blocking: list[ValidationIssue],
    review: list[ValidationIssue],
    clean_grade: LossQualityGrade,
) -> ValidationResult:
    blocking_issues = tuple(blocking)
    review_issues = tuple(review)
    if blocking_issues:
        quality_grade = LossQualityGrade.L0
    elif review_issues:
        quality_grade = LossQualityGrade.L3
    else:
        quality_grade = clean_grade
    return ValidationResult(
        valid=not blocking_issues,
        blocking_issues=blocking_issues,
        review_issues=review_issues,
        needs_review=bool(review_issues),
        quality_grade=quality_grade,
    )


def validate_loss_assessment(
    buildings: BuildingDamageResult,
    population: PopulationImpactResult,
    casualties: CasualtyResult,
    economic: EconomicLossResult,
    resources: ResourceDemandResult,
    *,
    coverage_ratio: float,
    grid_residuals: Sequence[GridResidual],
    context: ValidationContext,
) -> ValidationResult:
    _validate_context(context)

    blocking: list[ValidationIssue] = []
    review: list[ValidationIssue] = []

    _validate_missing_core_exposure(buildings, blocking)
    _validate_negative_metrics(
        buildings,
        population,
        casualties,
        economic,
        resources,
        blocking,
    )
    _validate_building_stock(buildings, blocking)
    _validate_shelter(population, resources, blocking)
    _validate_sum_scope(population, casualties, resources, blocking)

    coverage_valid = _validate_grid_structure(
        coverage_ratio,
        grid_residuals,
        _result_town_codes(buildings, population, casualties, economic),
        blocking,
    )
    if (
        coverage_valid
        and float(coverage_ratio) < context.minimum_town_coverage_ratio
    ):
        blocking.append(
            _issue(
                "coverage_below_minimum",
                "town coverage is below the region profile minimum",
                blocking=True,
                context={
                    "threshold": _context_value(
                        context.minimum_town_coverage_ratio
                    ),
                    "actual": _context_value(coverage_ratio),
                },
            )
        )

    for kind in ResourceKind:
        value = resources.values.get(kind)
        if (
            value is None
            or value.status != "available"
            or value.quantity is None
        ):
            blocking.append(
                _issue(
                    "required_parameter_unavailable",
                    f"resource demand for {kind.value} is unavailable",
                    blocking=True,
                    context={
                        "resource_kind": kind.value,
                        "reason": (
                            "missing_resource_kind"
                            if value is None
                            else value.reason
                        ),
                    },
                )
            )

    _validate_grid_review(
        grid_residuals,
        context.grid_residual_review_threshold,
        review,
    )
    _validate_stale_data_asset(context, review)
    _validate_review_flags(buildings, casualties, economic, review)

    return _build_result(
        blocking,
        review,
        _clean_quality_grade(buildings, population, casualties),
    )
