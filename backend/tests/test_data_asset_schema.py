import subprocess
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.data_assets.models import DataAssetSnapshot


BACKEND_DIR = Path(__file__).parents[1]
LATEST_REVISION = "0015_artifact_production"
DATA_ASSET_PREVIOUS_REVISION = "0011_intensity_assessment"
DATA_ASSET_TABLES = {
    "data_assets",
    "data_asset_versions",
    "data_asset_import_jobs",
    "data_asset_snapshots",
    "data_asset_audit_logs",
    "data_asset_records",
    "data_asset_rasters",
}


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["alembic", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _current_revision() -> str:
    result = _alembic("current")
    revision_lines = [
        line.strip().split()[0]
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("INFO")
    ]
    assert revision_lines
    return revision_lines[-1]


def _set_revision(target: str) -> None:
    current = _current_revision()
    if current == target:
        return

    alembic_config = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    scripts = ScriptDirectory.from_config(alembic_config)
    target_revision = scripts.get_revision(target)
    current_ancestors: set[str] = set()
    pending = [scripts.get_revision(current)]
    while pending:
        revision = pending.pop()
        if revision.revision in current_ancestors:
            continue
        current_ancestors.add(revision.revision)
        down_revisions = revision.down_revision
        if down_revisions is None:
            continue
        if isinstance(down_revisions, tuple):
            pending.extend(scripts.get_revision(item) for item in down_revisions)
        else:
            pending.append(scripts.get_revision(down_revisions))
    if target_revision.revision in current_ancestors:
        _alembic("downgrade", target)
    else:
        _alembic("upgrade", target)


async def _data_asset_schema_state() -> dict[str, object]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            run_columns = await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("assessment_runs")
                }
            )
            index_row = (
                await connection.execute(
                    text(
                        """
                        SELECT indexdef
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND indexname = 'uq_data_asset_versions_published_asset'
                        """
                    )
                )
            ).scalar_one_or_none()
            if "data_asset_snapshots" in tables:
                snapshot_columns = await connection.run_sync(
                    lambda sync: {
                        column["name"]
                        for column in inspect(sync).get_columns("data_asset_snapshots")
                    }
                )
                snapshot_unique = (
                    await connection.execute(
                        text(
                            """
                            SELECT pg_get_constraintdef(constraint_record.oid)
                            FROM pg_constraint AS constraint_record
                            JOIN pg_class AS target
                              ON target.oid = constraint_record.conrelid
                            JOIN pg_namespace AS target_schema
                              ON target_schema.oid = target.relnamespace
                            WHERE target_schema.nspname = current_schema()
                              AND target.relname = 'data_asset_snapshots'
                              AND constraint_record.conname =
                                  'uq_data_asset_snapshots_run_region_asset'
                            """
                        )
                    )
                ).scalar_one_or_none()
            else:
                snapshot_columns = set()
                snapshot_unique = None

            if "data_asset_versions" in tables:
                status_check = (
                    await connection.execute(
                        text(
                            """
                            SELECT pg_get_constraintdef(constraint_record.oid)
                            FROM pg_constraint AS constraint_record
                            JOIN pg_class AS target
                              ON target.oid = constraint_record.conrelid
                            JOIN pg_namespace AS target_schema
                              ON target_schema.oid = target.relnamespace
                            WHERE target_schema.nspname = current_schema()
                              AND target.relname = 'data_asset_versions'
                              AND constraint_record.conname =
                                  'ck_data_asset_versions_status'
                            """
                        )
                    )
                ).scalar_one_or_none()
                lifecycle_trigger = (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname
                            FROM pg_trigger
                            WHERE tgrelid = 'data_asset_versions'::regclass
                              AND tgname = 'trg_data_asset_versions_enforce_lifecycle'
                              AND NOT tgisinternal
                            """
                        )
                    )
                ).scalar_one_or_none()
                child_trigger = (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname
                            FROM pg_trigger
                            WHERE tgrelid = 'data_asset_records'::regclass
                              AND tgname = 'trg_data_asset_records_immutable'
                              AND NOT tgisinternal
                            """
                        )
                    )
                ).scalar_one_or_none()
                snapshot_trigger = (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname
                            FROM pg_trigger
                            WHERE tgrelid = 'data_asset_snapshots'::regclass
                              AND tgname = 'trg_data_asset_snapshots_validate_reference'
                              AND NOT tgisinternal
                            """
                        )
                    )
                ).scalar_one_or_none()
            else:
                status_check = None
                lifecycle_trigger = None
                child_trigger = None
                snapshot_trigger = None
        return {
            "tables": tables,
            "run_columns": run_columns,
            "published_index": index_row,
            "snapshot_columns": snapshot_columns,
            "snapshot_unique": snapshot_unique,
            "status_check": status_check,
            "lifecycle_trigger": lifecycle_trigger,
            "child_trigger": child_trigger,
            "snapshot_trigger": snapshot_trigger,
        }
    finally:
        await engine.dispose()


async def test_data_asset_center_migration_is_reversible() -> None:
    _set_revision(DATA_ASSET_PREVIOUS_REVISION)
    previous = await _data_asset_schema_state()
    assert DATA_ASSET_TABLES.isdisjoint(previous["tables"])
    assert {
        "data_asset_snapshot_fingerprint",
        "data_asset_snapshot_result",
    }.isdisjoint(previous["run_columns"])

    try:
        _set_revision(LATEST_REVISION)
        head = await _data_asset_schema_state()
        assert DATA_ASSET_TABLES <= head["tables"]
        assert {
            "data_asset_snapshot_fingerprint",
            "data_asset_snapshot_result",
        } <= head["run_columns"]
        assert head["published_index"] is not None
        assert "published" in head["published_index"]
        assert "CREATE UNIQUE INDEX" in head["published_index"]
        assert "region_id" in head["snapshot_columns"]
        assert head["snapshot_unique"] is not None
        assert "region_id" in head["snapshot_unique"]
        assert "run_id" in head["snapshot_unique"]
        assert "asset_key" in head["snapshot_unique"]
        assert head["status_check"] is not None
        assert head["lifecycle_trigger"] is not None
        assert head["child_trigger"] is not None
        assert head["snapshot_trigger"] is not None

        _set_revision(DATA_ASSET_PREVIOUS_REVISION)
        downgraded = await _data_asset_schema_state()
        assert DATA_ASSET_TABLES.isdisjoint(downgraded["tables"])
        assert {
            "data_asset_snapshot_fingerprint",
            "data_asset_snapshot_result",
        }.isdisjoint(downgraded["run_columns"])

        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)


def test_data_asset_snapshot_is_region_scoped() -> None:
    table = DataAssetSnapshot.__table__

    assert "region_id" in table.c
    snapshot_unique = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_data_asset_snapshots_run_region_asset"
    )
    assert [column.name for column in snapshot_unique.columns] == [
        "run_id",
        "region_id",
        "asset_key",
    ]


async def _seed_version(
    status: str,
    *,
    asset_key: str | None = None,
    region_id: str = "shanghai",
    version: str = "2022.1",
) -> uuid.UUID:
    engine = create_async_engine(settings.database_url)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset_key = asset_key or f"test.asset.{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO data_assets (
                    id,
                    asset_key,
                    region_id,
                    name,
                    data_type,
                    spatial_granularity,
                    responsibility_unit,
                    update_interval_days,
                    is_core,
                    contract
                )
                VALUES (
                    :asset_id,
                    :asset_key,
                    :region_id,
                    'Town',
                    'vector',
                    'town',
                    'owner',
                    365,
                    true,
                    CAST('{}' AS jsonb)
                )
                """
            ),
            {
                "asset_id": asset_id,
                "asset_key": asset_key,
                "region_id": region_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO data_asset_versions (
                    id,
                    asset_id,
                    version,
                    status,
                    source_uri,
                    schema_summary,
                    imported_by,
                    imported_at
                )
                VALUES (
                    :version_id,
                    :asset_id,
                    :version,
                    :status,
                    'https://example.gov.invalid/town.geojson',
                    CAST('{}' AS jsonb),
                    'reviewer',
                    now()
                )
                """
            ),
            {
                "version_id": version_id,
                "asset_id": asset_id,
                "version": version,
                "status": status,
            },
        )
    await engine.dispose()
    return version_id


async def test_data_asset_version_status_is_database_enforced() -> None:
    _set_revision(LATEST_REVISION)
    engine = create_async_engine(settings.database_url)
    try:
        with pytest.raises(
            IntegrityError,
            match="ck_data_asset_versions_status|invalid data asset version status",
        ):
            async with engine.begin() as connection:
                await _insert_version_with_connection(connection, status="bogus")
    finally:
        await engine.dispose()


async def test_published_version_is_immutable_and_lifecycle_is_reversible() -> None:
    _set_revision(LATEST_REVISION)
    version_id = await _seed_version("published")
    engine = create_async_engine(settings.database_url)
    try:
        with pytest.raises(IntegrityError, match="data_asset_version_is_immutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE data_asset_versions
                        SET checksum = :checksum
                        WHERE id = :version_id
                        """
                    ),
                    {"checksum": "a" * 64, "version_id": version_id},
                )

        with pytest.raises(IntegrityError, match="status transition"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE data_asset_versions
                        SET status = 'imported'
                        WHERE id = :version_id
                        """
                    ),
                    {"version_id": version_id},
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE data_asset_versions
                    SET status = 'retired',
                        retired_at = now(),
                        updated_at = now()
                    WHERE id = :version_id
                    """
                ),
                {"version_id": version_id},
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE data_asset_versions
                    SET status = 'published',
                        published_at = now(),
                        reviewed_by = 'reviewer',
                        updated_at = now()
                    WHERE id = :version_id
                    """
                ),
                {"version_id": version_id},
            )
    finally:
        await engine.dispose()


async def _insert_version_with_connection(connection, *, status: str) -> None:
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset_key = f"test.asset.{uuid.uuid4().hex}"
    await connection.execute(
        text(
            """
            INSERT INTO data_assets (
                id,
                asset_key,
                region_id,
                name,
                data_type,
                spatial_granularity,
                responsibility_unit,
                update_interval_days,
                is_core,
                contract
            )
            VALUES (
                :asset_id,
                :asset_key,
                'shanghai',
                'Town',
                'vector',
                'town',
                'owner',
                365,
                true,
                CAST('{}' AS jsonb)
            )
            """
        ),
        {"asset_id": asset_id, "asset_key": asset_key},
    )
    await connection.execute(
        text(
            """
            INSERT INTO data_asset_versions (
                id,
                asset_id,
                version,
                status,
                source_uri,
                schema_summary
            )
            VALUES (
                :version_id,
                :asset_id,
                '2022.1',
                :status,
                'https://example.gov.invalid/town.geojson',
                CAST('{}' AS jsonb)
            )
            """
        ),
        {"version_id": version_id, "asset_id": asset_id, "status": status},
    )
