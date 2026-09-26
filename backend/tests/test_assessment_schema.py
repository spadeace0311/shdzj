from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings


BACKEND_DIR = Path(__file__).parents[1]


def test_assessment_orm_metadata_contract() -> None:
    runs = AssessmentRun.__table__
    tasks = AssessmentTask.__table__

    assert {
        "id",
        "event_id",
        "revision_id",
        "outbox_id",
        "run_no",
        "trigger_reason",
        "status",
        "priority",
        "t1_at",
        "deadline_at",
        "started_at",
        "completed_at",
        "snapshot",
        "last_error",
        "created_at",
        "updated_at",
    } <= set(runs.c.keys())
    assert {
        "id",
        "run_id",
        "task_key",
        "task_type",
        "component",
        "status",
        "priority",
        "sequence",
        "deadline_at",
        "started_at",
        "completed_at",
        "attempt_count",
        "max_attempts",
        "result",
        "last_error",
        "created_at",
        "updated_at",
    } <= set(tasks.c.keys())

    assert {
        "earthquake_events.id",
        "earthquake_revisions.id",
        "event_lifecycle_outbox.id",
    } == {foreign_key.target_fullname for foreign_key in runs.foreign_keys}
    assert {"assessment_runs.id"} == {
        foreign_key.target_fullname for foreign_key in tasks.foreign_keys
    }

    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["revision_id"]
        for constraint in runs.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["outbox_id"]
        for constraint in runs.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["event_id", "run_no"]
        for constraint in runs.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["run_id", "task_key"]
        for constraint in tasks.constraints
    )

    assert any(
        index.name == "ix_assessment_runs_status_deadline"
        and [column.name for column in index.columns] == ["status", "deadline_at"]
        for index in runs.indexes
    )
    assert any(
        index.name == "ix_assessment_tasks_status_deadline"
        and [column.name for column in index.columns] == ["status", "deadline_at"]
        for index in tasks.indexes
    )

    assert runs.c.status.default.arg == "pending"
    assert tasks.c.status.default.arg == "pending"
    assert runs.c.priority.default.arg == 100
    assert tasks.c.priority.default.arg == 100
    assert tasks.c.attempt_count.default.arg == 0
    assert tasks.c.max_attempts.default.arg == 3


async def test_assessment_orchestration_schema_contract() -> None:
    alembic_config = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    expected_migration_version = ScriptDirectory.from_config(alembic_config).get_current_head()

    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema = await connection.run_sync(
            lambda sync: {
                "tables": set(inspect(sync).get_table_names()),
                "columns": {
                    table: {column["name"] for column in inspect(sync).get_columns(table)}
                    for table in ("assessment_runs", "assessment_tasks")
                    if table in inspect(sync).get_table_names()
                },
            }
        )
        migration_version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()

    assert migration_version == expected_migration_version
    assert {"assessment_runs", "assessment_tasks"} <= schema["tables"]
    assert {
        "event_id",
        "revision_id",
        "outbox_id",
        "run_no",
        "status",
        "priority",
        "t1_at",
        "deadline_at",
        "snapshot",
    } <= schema["columns"]["assessment_runs"]
    assert {
        "run_id",
        "task_key",
        "task_type",
        "component",
        "status",
        "priority",
        "sequence",
        "deadline_at",
        "attempt_count",
        "max_attempts",
    } <= schema["columns"]["assessment_tasks"]
