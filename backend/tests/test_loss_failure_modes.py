from app.loss.domain import ResourceKind
from tests.loss_helpers import (
    run_buildings_twice,
    run_correction,
    run_formal,
    run_partial_asset_publish_recovery,
    run_resources_with_missing,
    run_without_vulnerability_row,
)


async def test_missing_resource_parameter_does_not_block_other_resources(
    session_factory,
) -> None:
    result = await run_resources_with_missing(session_factory, "tent.base")
    assert result.values[ResourceKind.TENT].status == "unavailable"
    assert result.values[ResourceKind.FOOD].status == "available"
    assert result.persisted_values["tent.quantity"].numeric_value is None
    assert result.persisted_values["tent.quantity"].value_status == "unavailable"
    assert result.persisted_values["food.quantity"].numeric_value is not None


async def test_missing_vulnerability_row_fails_run(session_factory) -> None:
    result = await run_without_vulnerability_row(session_factory)
    assert result.status == "failed"


async def test_duplicate_activity_delivery_publishes_once(session_factory) -> None:
    first, second = await run_buildings_twice(session_factory)
    assert first.product_id == second.product_id


async def test_correction_creates_independent_loss_version(
    session_factory,
) -> None:
    first = await run_formal(session_factory)
    second = await run_correction(session_factory)
    assert first.product_id != second.product_id
    assert first.run_id != second.run_id


async def test_partial_asset_publish_restores_previous_published_versions(
    session_factory,
) -> None:
    await run_partial_asset_publish_recovery(session_factory)
