from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def test_loss_tables_and_constraints_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            assert {
                "loss_model_definitions",
                "loss_parameter_sets",
                "loss_products",
                "loss_metric_values",
                "loss_product_rasters",
            } <= tables
            constraints = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'loss_model_definitions'::regclass,
                            'loss_parameter_sets'::regclass,
                            'loss_products'::regclass,
                            'loss_metric_values'::regclass,
                            'loss_product_rasters'::regclass
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {
        row["conname"]: row["definition"]
        for row in constraints
    }
    assert "UNIQUE (model_id, formula_version)" in definitions[
        "uq_loss_model_formula"
    ]
    assert "UNIQUE (parameter_set_id, version)" in definitions[
        "uq_loss_parameter_version"
    ]
    assert "UNIQUE (run_id, product_type)" in definitions[
        "uq_loss_product_run_type"
    ]
    assert "UNIQUE (product_id, area_scope, area_code, metric_key, value_type)" in (
        definitions["uq_loss_metric_value"]
    )
    assert "UNIQUE (product_id, raster_version)" in definitions[
        "uq_loss_product_raster_version"
    ]


async def test_loss_product_uses_known_status_and_quality_values() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conname IN (
                            'ck_loss_product_status_value',
                            'ck_loss_product_quality_value'
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["conname"]: row["definition"] for row in rows}
    status_definition = definitions["ck_loss_product_status_value"]
    for value in ("complete", "partial", "unavailable", "invalid"):
        assert f"'{value}'" in status_definition
    quality_definition = definitions["ck_loss_product_quality_value"]
    for value in ("L1", "L2", "L3", "L0"):
        assert f"'{value}'" in quality_definition
