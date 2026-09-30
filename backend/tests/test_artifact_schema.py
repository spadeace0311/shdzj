from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import configure_mappers

from app.artifacts.models import ArtifactTemplate
from app.config import settings

ARTIFACT_TABLES = {
    "artifact_templates",
    "artifact_template_versions",
    "production_input_snapshots",
    "production_input_snapshot_items",
    "artifact_production_runs",
    "artifact_production_tasks",
    "artifact_task_dependency_bindings",
    "generated_artifacts",
    "artifact_publications",
    "artifact_override_requests",
}


async def test_artifact_tables_and_nullable_t1_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            t1_column = await connection.run_sync(
                lambda sync: next(
                    column
                    for column in inspect(sync).get_columns("assessment_runs")
                    if column["name"] == "t1_at"
                )
            )
    finally:
        await engine.dispose()

    assert ARTIFACT_TABLES <= tables
    assert t1_column["nullable"] is True


async def test_artifact_partial_unique_indexes_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND indexname IN (
                            'uq_artifact_run_current_scope',
                            'uq_artifact_publication_current',
                            'uq_artifact_dependency_product',
                            'uq_artifact_dependency_artifact'
                          )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert "WHERE" in definitions["uq_artifact_run_current_scope"]
    assert "is_current" in definitions["uq_artifact_run_current_scope"]
    assert "superseded_at IS NULL" in definitions["uq_artifact_publication_current"]
    assert "dependency_kind = 'assessment_product'" in (
        definitions["uq_artifact_dependency_product"]
    )
    assert "dependency_kind = 'artifact'" in definitions["uq_artifact_dependency_artifact"]


def test_artifact_orm_mappers_configure() -> None:
    assert ArtifactTemplate.__tablename__ == "artifact_templates"
    configure_mappers()
