from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from geoalchemy2 import Geometry, Raster
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.intensity.models import (
    AssessmentTaskAttempt,
    IntensityFieldProduct,
    IntensityRaster,
)


BACKEND_DIR = Path(__file__).parents[1]


def test_intensity_orm_metadata_contract() -> None:
    attempt = AssessmentTaskAttempt.__table__
    product = IntensityFieldProduct.__table__
    raster = IntensityRaster.__table__

    assert {
        "id",
        "task_id",
        "attempt_number",
        "status",
        "started_at",
        "completed_at",
        "input_fingerprint",
        "output_checksum",
        "error_category",
        "error_summary",
        "created_at",
    } <= set(attempt.c.keys())
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
    assert isinstance(product.c.spatial_extent.type, Geometry)

    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns]
        == ["task_id", "attempt_number"]
        for constraint in attempt.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["run_id", "product_type"]
        for constraint in product.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["product_id"]
        for constraint in raster.constraints
    )

    assert {foreign_key.target_fullname for foreign_key in attempt.foreign_keys} == {
        "assessment_tasks.id"
    }
    assert {foreign_key.target_fullname for foreign_key in product.foreign_keys} == {
        "assessment_runs.id",
        "assessment_tasks.id",
    }
    assert {foreign_key.target_fullname for foreign_key in raster.foreign_keys} == {
        "intensity_field_products.id"
    }


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
    assert {
        "id",
        "task_id",
        "attempt_number",
        "status",
        "started_at",
        "completed_at",
        "input_fingerprint",
        "output_checksum",
        "error_category",
        "error_summary",
        "created_at",
    } <= schema["columns"]["assessment_task_attempts"]
    assert {
        "id",
        "run_id",
        "task_id",
        "product_type",
        "status",
        "algorithm_version",
        "parameter_version",
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
    } <= schema["columns"]["intensity_field_products"]
    assert {
        "id",
        "product_id",
        "rast",
        "band_manifest",
        "checksum",
        "width",
        "height",
        "srid",
    } <= schema["columns"]["intensity_rasters"]
