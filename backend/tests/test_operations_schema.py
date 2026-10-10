from sqlalchemy import inspect

from app.db import engine


OPERATIONS_TABLES = {
    "operations_settings",
    "operations_test_schedules",
    "operations_runs",
    "operations_audit_logs",
}


async def test_operations_tables_exist() -> None:
    async with engine.connect() as connection:
        names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert OPERATIONS_TABLES <= names
