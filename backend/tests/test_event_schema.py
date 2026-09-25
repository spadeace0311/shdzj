from geoalchemy2 import Geometry
from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import Base
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage


def test_event_model_metadata_matches_foundation_contract() -> None:
    assert {
        "raw_messages",
        "earthquake_events",
        "earthquake_revisions",
    } <= set(Base.metadata.tables)

    raw_messages = RawMessage.__table__
    events = EarthquakeEvent.__table__
    revisions = EarthquakeRevision.__table__

    assert isinstance(events.c.geom.type, Geometry)
    assert events.c.geom.type.geometry_type == "POINT"
    assert events.c.geom.type.srid == 4326
    assert _has_unique_index(events, "canonical_source_id")
    assert _has_unique_index(revisions, "raw_message_id")
    assert any(
        index.dialect_options["postgresql"].get("using") == "gist"
        for index in events.indexes
        if [column.name for column in index.columns] == ["geom"]
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["event_id", "revision_no"]
        for constraint in revisions.constraints
    )
    assert any(
        index.unique
        and [column.name for column in index.columns] == ["event_id"]
        and str(index.dialect_options["postgresql"].get("where")) == "is_current"
        for index in revisions.indexes
    )

    for column in (
        raw_messages.c.received_at,
        events.c.created_at,
        events.c.updated_at,
        revisions.c.created_at,
    ):
        assert str(column.server_default.arg) == "now()"


def _has_unique_index(table, column_name: str) -> bool:
    return any(
        index.unique and [column.name for column in index.columns] == [column_name]
        for index in table.indexes
    )


async def test_event_tables_and_postgis_exist() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        extension = await connection.exec_driver_sql(
            "SELECT extname FROM pg_extension WHERE extname = 'postgis'"
        )

    await engine.dispose()

    assert {"raw_messages", "earthquake_events", "earthquake_revisions"} <= table_names
    assert extension.scalar_one() == "postgis"
