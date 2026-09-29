from app.loss.domain import LossProductType
from app.loss.models import LossProductRaster
from app.loss.repository import LossRepository
from sqlalchemy import select
from tests.loss_helpers import (
    _cleanup_loss_fixture,
    _execute_loss_workflow,
    _publish_fixed_assets,
    _request_loss_api,
    _seed_loss_boundary,
    _seed_loss_run,
)


async def test_fixed_shanghai_loss_chain_publishes_all_products(
    session_factory,
) -> None:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )

        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        assert workflow_result.status == "completed"

        response = await _request_loss_api(seeded.run_id)
        assert response.status_code == 200
        product_map = {
            product["product_type"]: product
            for product in response.json()["products"]
        }

        async with session_factory() as session:
            validation_inputs = await LossRepository().load_validation_inputs(
                session,
                seeded.run_id,
            )
            building_product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
            assert building_product is not None
            raster_row = await session.scalar(
                select(LossProductRaster).where(
                    LossProductRaster.product_id == building_product.id
                )
            )

        assert product_map["building_damage"]["status"] == "complete"
        assert product_map["population_impact"]["status"] == "complete"
        assert product_map["casualties"]["status"] == "complete"
        assert product_map["economic_loss"]["status"] == "complete"
        assert product_map["resource_demand"]["status"] == "complete"
        assert product_map["validation"]["status"] == "complete"
        assert product_map["building_damage"]["quality_grade"] in {"L2", "L3"}
        assert product_map["building_damage"]["calibration_status"] == "uncalibrated"
        assert raster_row.spatial_allocation_rule == "town-uniform-v1"
        assert all(
            abs(residual.residual) <= 1e-6
            for residual in validation_inputs.grid_residuals
        )
        assert {
            metric["value_status"]
            for metric in product_map["resource_demand"]["metrics"]
            if metric["metric_key"] == "tent.quantity"
        } == {"available"}
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )
