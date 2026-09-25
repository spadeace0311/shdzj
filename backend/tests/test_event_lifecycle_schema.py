from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.events import models as event_models
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage


BACKEND_DIR = Path(__file__).parents[1]


def test_event_lifecycle_orm_metadata_contract() -> None:
    assert hasattr(event_models, "EventLifecycleOutbox")

    raw_messages = RawMessage.__table__
    events = EarthquakeEvent.__table__
    revisions = EarthquakeRevision.__table__
    outbox = event_models.EventLifecycleOutbox.__table__

    assert {"provider", "ingest_lane"} <= set(raw_messages.c.keys())
    assert {"t1_at", "lifecycle_state", "latest_trigger_revision_id"} <= set(events.c.keys())
    assert {
        "semantic_fingerprint",
        "provider",
        "ingest_lane",
        "ingested_at",
        "inside_shanghai",
        "distance_to_boundary_km",
        "region_boundary_version",
        "region_computed_at",
    } <= set(revisions.c.keys())
    assert revisions.c.distance_to_boundary_km.type.precision == 10
    assert revisions.c.distance_to_boundary_km.type.scale == 3

    semantic_index = next(
        index
        for index in revisions.indexes
        if index.name == "uq_earthquake_revisions_semantic_fingerprint"
    )
    assert semantic_index.unique is True
    assert [column.name for column in semantic_index.columns] == [
        "event_id",
        "semantic_fingerprint",
    ]
    assert (
        str(semantic_index.dialect_options["postgresql"].get("where"))
        == "semantic_fingerprint IS NOT NULL"
    )

    assert {
        "event_id",
        "revision_id",
        "trigger_type",
        "trigger_reason",
        "payload",
        "status",
        "attempt_count",
        "created_at",
        "available_at",
        "published_at",
        "last_error",
    } <= set(outbox.c.keys())
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns]
        == ["event_id", "revision_id", "trigger_type"]
        for constraint in outbox.constraints
    )
    assert any(
        index.name == "ix_event_lifecycle_outbox_pending"
        and [column.name for column in index.columns] == ["status", "available_at"]
        for index in outbox.indexes
    )
    assert {
        foreign_key.target_fullname
        for foreign_key in outbox.foreign_keys
    } == {"earthquake_events.id", "earthquake_revisions.id"}


async def test_event_lifecycle_schema_contract() -> None:
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
                    for table in (
                        "raw_messages",
                        "earthquake_events",
                        "earthquake_revisions",
                        "event_lifecycle_outbox",
                    )
                    if table in inspect(sync).get_table_names()
                },
            }
        )
        index_rows = (
            await connection.execute(
                text(
                    "SELECT indexname, indexdef "
                    "FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = "
                    "'uq_earthquake_revisions_semantic_fingerprint'"
                )
            )
        ).mappings().all()
        migration_version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()

    assert migration_version == expected_migration_version
    assert "event_lifecycle_outbox" in schema["tables"]
    assert {"provider", "ingest_lane"} <= schema["columns"]["raw_messages"]
    assert {"t1_at", "lifecycle_state", "latest_trigger_revision_id"} <= schema["columns"][
        "earthquake_events"
    ]
    assert {
        "semantic_fingerprint",
        "provider",
        "ingest_lane",
        "ingested_at",
        "inside_shanghai",
        "distance_to_boundary_km",
        "region_boundary_version",
        "region_computed_at",
    } <= schema["columns"]["earthquake_revisions"]
    assert {
        "event_id",
        "revision_id",
        "trigger_type",
        "trigger_reason",
        "status",
    } <= schema["columns"]["event_lifecycle_outbox"]

    assert len(index_rows) == 1
    index_definition = index_rows[0]["indexdef"]
    assert "CREATE UNIQUE INDEX" in index_definition
    assert "(event_id, semantic_fingerprint)" in index_definition
    assert "WHERE (semantic_fingerprint IS NOT NULL)" in index_definition
