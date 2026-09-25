from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


EXPECTED_TABLES = {"collector_runtime_state", "collector_dead_letters"}


def _inspect_collector_schema(sync_connection: Connection) -> dict[str, Any]:
    inspector = inspect(sync_connection)
    tables = set(inspector.get_table_names())
    tables_to_inspect = EXPECTED_TABLES & tables

    return {
        "tables": tables,
        "columns": {
            table: {
                column["name"]: column for column in inspector.get_columns(table)
            }
            for table in tables_to_inspect
        },
        "primary_keys": {
            table: inspector.get_pk_constraint(table)["constrained_columns"]
            for table in tables_to_inspect
        },
        "indexes": {
            table: {
                index["name"]: index for index in inspector.get_indexes(table)
            }
            for table in tables_to_inspect
        },
    }


async def test_collector_schema_contract() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        schema = await connection.run_sync(_inspect_collector_schema)
        migration_version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()

    assert migration_version == "0006_collector_runtime"
    assert EXPECTED_TABLES <= schema["tables"]

    runtime_columns = schema["columns"]["collector_runtime_state"]
    assert set(runtime_columns) == {
        "provider",
        "state",
        "connected",
        "last_http_status",
        "last_connected_at",
        "last_message_at",
        "last_success_at",
        "last_processed_source_time",
        "consecutive_failures",
        "reconnect_count",
        "last_error",
        "updated_at",
    }
    assert schema["primary_keys"]["collector_runtime_state"] == ["provider"]
    assert runtime_columns["provider"]["nullable"] is False
    assert runtime_columns["state"]["nullable"] is False
    assert runtime_columns["connected"]["nullable"] is False
    assert str(runtime_columns["connected"]["default"]) == "false"
    assert runtime_columns["consecutive_failures"]["nullable"] is False
    assert str(runtime_columns["consecutive_failures"]["default"]) == "0"
    assert runtime_columns["reconnect_count"]["nullable"] is False
    assert str(runtime_columns["reconnect_count"]["default"]) == "0"
    assert runtime_columns["updated_at"]["nullable"] is False
    assert str(runtime_columns["updated_at"]["default"]) == "now()"
    for column_name in (
        "last_connected_at",
        "last_message_at",
        "last_success_at",
        "last_processed_source_time",
    ):
        assert runtime_columns[column_name]["nullable"] is True
        assert runtime_columns[column_name]["type"].timezone is True
    assert {
        "ix_collector_runtime_state_state",
    } <= set(schema["indexes"]["collector_runtime_state"])

    dead_letter_columns = schema["columns"]["collector_dead_letters"]
    assert set(dead_letter_columns) == {
        "id",
        "provider",
        "lane",
        "source_message_id",
        "raw_payload",
        "received_at",
        "error_category",
        "error_message",
        "status",
        "first_failed_at",
        "last_failed_at",
    }
    assert schema["primary_keys"]["collector_dead_letters"] == ["id"]
    assert dead_letter_columns["id"]["nullable"] is False
    assert dead_letter_columns["raw_payload"]["nullable"] is False
    assert dead_letter_columns["raw_payload"]["type"].__class__.__name__ == "JSONB"
    for column_name in (
        "provider",
        "lane",
        "received_at",
        "error_category",
        "error_message",
        "status",
        "first_failed_at",
        "last_failed_at",
    ):
        assert dead_letter_columns[column_name]["nullable"] is False
    assert dead_letter_columns["source_message_id"]["nullable"] is True
    for column_name in ("received_at", "first_failed_at", "last_failed_at"):
        assert dead_letter_columns[column_name]["type"].timezone is True
    assert {
        "ix_collector_dead_letters_provider",
        "ix_collector_dead_letters_lane",
        "ix_collector_dead_letters_source_message_id",
        "ix_collector_dead_letters_error_category",
        "ix_collector_dead_letters_status",
        "ix_collector_dead_letters_last_failed_at",
        "ix_collector_dead_letters_open",
    } <= set(schema["indexes"]["collector_dead_letters"])
    assert schema["indexes"]["collector_dead_letters"]["ix_collector_dead_letters_open"][
        "column_names"
    ] == ["status", "last_failed_at"]
