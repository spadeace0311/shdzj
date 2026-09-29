from dataclasses import replace

import pytest

from app.loss.domain import LossProductType, ResourceKind
from app.loss.resources import ResourceDemandValue
from app.loss.validation import (
    GridResidual,
    ValidationContext,
    validate_loss_assessment,
)
from tests.loss_factories import (
    building_damage_result,
    casualty_result,
    economic_loss_result,
    population_impact_result,
    resource_demand_result,
)


def _context() -> ValidationContext:
    return ValidationContext(
        region_profile_version="shanghai-loss-region-v1",
        minimum_town_coverage_ratio=0.95,
        grid_residual_review_threshold=0.01,
        data_asset_snapshot_fingerprint="f" * 64,
        product_snapshot_fingerprints={
            product_type: "f" * 64
            for product_type in (
                LossProductType.BUILDING_DAMAGE,
                LossProductType.POPULATION_IMPACT,
                LossProductType.CASUALTIES,
                LossProductType.ECONOMIC_LOSS,
                LossProductType.RESOURCE_DEMAND,
            )
        },
    )


def _codes(result) -> tuple[str, ...]:
    return tuple(issue.code for issue in result.blocking_issues)


def _review_codes(result) -> tuple[str, ...]:
    return tuple(issue.code for issue in result.review_issues)


def test_valid_results_have_no_blocking_issues() -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=0.99,
        grid_residuals=(
            GridResidual("town", "t1", 0.0),
            GridResidual("town", "t2", 0.0),
        ),
        context=_context(),
    )

    assert result.valid is True
    assert result.blocking_issues == ()
    assert "fallback_model_used" in _review_codes(result)
    assert "partial_scope" in _review_codes(result)


def _with_building_negative_area():
    result = building_damage_result()
    state = replace(result.towns["t1"].states[0], area_m2=-1.0)
    town = replace(
        result.towns["t1"],
        states=(state, *result.towns["t1"].states[1:]),
    )
    return replace(result, towns={"t1": town})


def _with_negative_population():
    result = population_impact_result()
    town = replace(result.towns["t1"], affected_population=-1.0)
    return replace(result, towns={"t1": town})


def _with_negative_casualty():
    result = casualty_result()
    town = replace(result.towns["t1"], deaths=-1.0)
    return replace(result, towns={"t1": town})


def _with_negative_economic_loss():
    result = economic_loss_result()
    town = replace(result.towns["t1"], total_loss_yuan=-1.0)
    return replace(result, towns={"t1": town})


@pytest.mark.parametrize(
    "buildings, population, casualties, economic",
    [
        (_with_building_negative_area(), population_impact_result(), casualty_result(), economic_loss_result()),
        (building_damage_result(), _with_negative_population(), casualty_result(), economic_loss_result()),
        (building_damage_result(), population_impact_result(), _with_negative_casualty(), economic_loss_result()),
        (building_damage_result(), population_impact_result(), casualty_result(), _with_negative_economic_loss()),
    ],
)
def test_negative_metric_is_blocking(buildings, population, casualties, economic) -> None:
    result = validate_loss_assessment(
        buildings,
        population,
        casualties,
        economic,
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "negative_metric" in _codes(result)
    assert result.quality_grade.value == "L0"


def test_building_damage_exceeding_stock_is_blocking() -> None:
    buildings = building_damage_result()
    town = buildings.towns["t1"]
    state_total = sum(state.area_m2 for state in town.states)
    invalid_town = replace(town, total_area_m2=state_total - 1.0)
    result = validate_loss_assessment(
        replace(buildings, towns={"t1": invalid_town}),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "building_damage_exceeds_stock" in _codes(result)


def test_empty_building_result_is_missing_core_exposure() -> None:
    result = validate_loss_assessment(
        replace(building_damage_result(), towns={}),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    issue = next(
        issue
        for issue in result.blocking_issues
        if issue.code == "missing_core_exposure"
    )
    assert issue.context["town_code"] is None
    assert issue.context["town_count"] == 0
    assert result.quality_grade.value == "L0"


def test_state_less_building_town_is_missing_core_exposure() -> None:
    buildings = building_damage_result()
    town = replace(buildings.towns["t1"], states=())
    result = validate_loss_assessment(
        replace(buildings, towns={"t1": town}),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    issue = next(
        issue
        for issue in result.blocking_issues
        if issue.code == "missing_core_exposure"
    )
    assert issue.context["town_code"] == "t1"
    assert issue.context["state_count"] == 0
    assert result.quality_grade.value == "L0"


def test_non_positive_building_stock_is_missing_core_exposure() -> None:
    buildings = building_damage_result()
    town = replace(buildings.towns["t1"], total_area_m2=0.0)
    result = validate_loss_assessment(
        replace(buildings, towns={"t1": town}),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    issue = next(
        issue
        for issue in result.blocking_issues
        if issue.code == "missing_core_exposure"
    )
    assert issue.context["town_code"] == "t1"
    assert issue.context["total_area_m2"] == 0.0
    assert result.quality_grade.value == "L0"


def test_shelter_above_affected_population_is_blocking() -> None:
    population = population_impact_result()
    town = replace(
        population.towns["t1"],
        affected_population=900.0,
        emergency_shelter_population=1000.0,
    )
    result = validate_loss_assessment(
        building_damage_result(),
        replace(population, towns={"t1": town}),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "shelter_exceeds_affected" in _codes(result)


def test_resource_aggregate_sum_mismatch_is_blocking() -> None:
    resources = resource_demand_result()
    invalid_inputs = replace(resources.inputs, deaths=99.0)
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        replace(resources, inputs=invalid_inputs),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "sum_scope_mismatch" in _codes(result)


def test_coverage_below_minimum_is_blocking() -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=0.94,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "coverage_below_minimum" in _codes(result)


@pytest.mark.parametrize("coverage_ratio", [float("nan"), float("inf"), -0.01, 1.01])
def test_out_of_range_or_non_finite_coverage_is_grid_mismatch(
    coverage_ratio: float,
) -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=coverage_ratio,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    assert "grid_total_mismatch" in _codes(result)
    assert "coverage_below_minimum" not in _codes(result)


@pytest.mark.parametrize(
    "grid_residuals",
    [
        (GridResidual("town", "t1", float("nan")),),
        (GridResidual("", "t1", 0.0),),
        (GridResidual("town", "", 0.0),),
        (GridResidual("town", "t2", 0.0),),
        (
            GridResidual("town", "t1", 0.0),
            GridResidual("town", "t1", 0.0),
        ),
    ],
)
def test_structurally_invalid_residuals_are_grid_mismatch(grid_residuals) -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=grid_residuals,
        context=_context(),
    )

    assert result.valid is False
    assert "grid_total_mismatch" in _codes(result)
    assert "grid_residual_threshold" not in _review_codes(result)


def test_maximum_grid_residual_marks_review_without_rewriting_values() -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(
            GridResidual("town", "t1", 0.0),
            GridResidual("town", "t2", 0.02),
        ),
        context=_context(),
    )

    assert result.valid is True
    assert result.needs_review is True
    assert "grid_residual_threshold" in _review_codes(result)
    assert result.quality_grade.value == "L3"


def test_stale_data_asset_is_review() -> None:
    context = replace(
        _context(),
        product_snapshot_fingerprints={
            LossProductType.BUILDING_DAMAGE: "0" * 64,
            LossProductType.POPULATION_IMPACT: "f" * 64,
            LossProductType.CASUALTIES: "f" * 64,
            LossProductType.ECONOMIC_LOSS: "f" * 64,
            LossProductType.RESOURCE_DEMAND: "f" * 64,
        },
    )
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=context,
    )

    assert result.valid is True
    assert result.needs_review is True
    assert "stale_data_asset" in _review_codes(result)


def test_fallback_model_used_is_review() -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is True
    assert result.needs_review is True
    assert "fallback_model_used" in _review_codes(result)


def test_partial_scope_is_review() -> None:
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        resource_demand_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is True
    assert result.needs_review is True
    assert "partial_scope" in _review_codes(result)


def test_unavailable_resource_parameter_is_blocking() -> None:
    resources = resource_demand_result()
    values = dict(resources.values)
    values[ResourceKind.TENT] = ResourceDemandValue(
        quantity=None,
        status="unavailable",
        reason="missing_parameter:tent.base",
    )
    result = validate_loss_assessment(
        building_damage_result(),
        population_impact_result(),
        casualty_result(),
        economic_loss_result(),
        replace(resources, values=values),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=_context(),
    )

    assert result.valid is False
    issue = next(
        issue
        for issue in result.blocking_issues
        if issue.code == "required_parameter_unavailable"
    )
    assert issue.context["resource_kind"] == "tent"
    assert issue.context["reason"] == "missing_parameter:tent.base"
    assert resources.values[ResourceKind.DRINKING_WATER].quantity is not None
