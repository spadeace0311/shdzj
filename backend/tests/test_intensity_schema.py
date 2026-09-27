from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from geoalchemy2 import Raster
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.intensity.models import IntensityFieldProduct, IntensityRaster


BACKEND_DIR = Path(__file__).parents[1]


def test_intensity_orm_metadata_contract() -> None:
    product = IntensityFieldProduct.__table__
    raster = IntensityRaster.__table__

    assert {
        "run_id",
        "task_id",
        "product_type",
        "status",
        "algorithm_version",
        "parameter_version",
        "strategy_version",
        "grid_definition_version",
        "region_profile_version",
        "input_fingerprint",
        "input_checksum",
        "output_checksum",
        "quality_grade",
        "coverage_ratio",
        "spatial_extent",
        "statistics",
        "source_product_id",
        "observed_at",
        "completed_at",
        "published_at",
    } <= set(product.c.keys())
    assert {
        "product_id",
        "rast",
        "band_manifest",
        "checksum",
        "width",
        "height",
        "srid",
    } <= set(raster.c.keys())
    assert isinstance(raster.c.rast.type, Raster)


async def test_intensity_schema_exists_at_migration_head() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    expected = ScriptDirectory.from_config(config).get_current_head()

    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema: dict[str, Any] = await connection.run_sync(
            lambda sync: {
                "tables": set(inspect(sync).get_table_names()),
                "columns": {
                    table: {column["name"] for column in inspect(sync).get_columns(table)}
                    for table in (
                        "assessment_runs",
                        "assessment_tasks",
                        "assessment_task_attempts",
                        "intensity_field_products",
                        "intensity_rasters",
                        "earthquake_events",
                    )
                    if table in inspect(sync).get_table_names()
                },
            }
        )
        migration = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        raster_extension = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis_raster')")
        )
    await engine.dispose()

    assert migration == expected
    assert raster_extension is True
    assert {
        "assessment_task_attempts",
        "intensity_field_products",
        "intensity_rasters",
    } <= schema["tables"]
    assert {
        "report_ingested_at",
        "deadline_basis_at",
        "deadline_exceeded_at",
        "duration_ms",
        "algorithm_bundle_version",
        "superseded_by_run_id",
        "superseded_at",
    } <= schema["columns"]["assessment_runs"]
    assert {
        "input_fingerprint",
        "output_checksum",
        "algorithm_version",
    } <= schema["columns"]["assessment_tasks"]
    assert {
        "latest_assessment_run_id",
        "effective_assessment_run_id",
    } <= schema["columns"]["earthquake_events"]
