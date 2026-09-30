import ast
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

BACKEND_DIR = Path(__file__).parents[1]
MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations" / "versions"
ALEMBIC_VERSION_LENGTH = 32
LATEST_REVISION = "0015_artifact_production"
INTENSITY_PREVIOUS_REVISION = "0010_assessment_orchestration"
DATA_ASSET_PREVIOUS_REVISION = "0011_intensity_assessment"
LOSS_PREVIOUS_REVISION = "0013_data_asset_final_fixes"
ARTIFACT_PREVIOUS_REVISION = "0014_loss_assessment"
NON_CENC_REVISION = "0009_non_cenc_lifecycle"
REGION_MARITIME_REVISION = "0008_region_boundaries_maritime"
OLD_REGION_REVISION = "0007_region_boundaries"
MIGRATION_TEST_VERSION = "migration-test-0008"
NON_CENC_MIGRATION_TEST_PREFIX = "migration-test-noncenc-"
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
LOSS_TABLES = {
    "loss_model_definitions",
    "loss_parameter_sets",
    "loss_products",
    "loss_metric_values",
    "loss_product_rasters",
}
_USE_DATABASE_DEFAULT = object()


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


async def _delete_migration_test_rows() -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM region_boundaries WHERE version = :version"),
                {"version": MIGRATION_TEST_VERSION},
            )
    finally:
        await engine.dispose()


async def _insert_old_region_boundary(
    *,
    source_uri: str | None,
    checksum: str | None,
) -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO region_boundaries (
                        id,
                        version,
                        name,
                        local_buffer_km,
                        geom,
                        source_uri,
                        checksum,
                        is_active
                    )
                    VALUES (
                        CAST(:id AS uuid),
                        :version,
                        'migration-test',
                        50,
                        ST_GeomFromText(
                            'MULTIPOLYGON (((120.8 30.6, 122.2 30.6, 122.2 31.9,
                                             120.8 31.9, 120.8 30.6)))',
                            4326
                        ),
                        :source_uri,
                        :checksum,
                        false
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "version": MIGRATION_TEST_VERSION,
                    "source_uri": source_uri,
                    "checksum": checksum,
                },
            )
    finally:
        await engine.dispose()


async def _region_boundary_state(
    *,
    include_maritime: bool = True,
) -> dict[str, object]:
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(_inspect_region_columns)
            maritime_expression = (
                "ST_IsEmpty(maritime_geom)" if include_maritime else "NULL::boolean"
            )
            row = (
                (
                    await connection.execute(
                        text(
                            f"""
                        SELECT
                            source_uri,
                            checksum,
                            {maritime_expression} AS maritime_empty,
                            ST_Covers(
                                geom,
                                ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326)
                            ) AS administrative_covers
                        FROM region_boundaries
                        WHERE version = :version
                        """
                        ),
                        {"version": MIGRATION_TEST_VERSION},
                    )
                )
                .mappings()
                .one()
            )
        return {"columns": columns, "row": dict(row)}
    finally:
        await engine.dispose()


def _inspect_region_columns(connection) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(connection).get_columns("region_boundaries")
    }


async def _delete_non_cenc_migration_test_rows() -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM earthquake_events
                    WHERE canonical_source_id LIKE :prefix
                    """
                ),
                {"prefix": f"{NON_CENC_MIGRATION_TEST_PREFIX}%"},
            )
    finally:
        await engine.dispose()


async def _insert_non_cenc_migration_test_event(
    *,
    suffix: str,
    event_type: str,
    lifecycle_state: str | None | object = "auto_pending",
    t1_at: str | None = "2026-09-25T01:00:00+00:00",
) -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.begin() as connection:
            parameters = {
                "id": str(uuid.uuid4()),
                "source": "test",
                "canonical_source_id": f"{NON_CENC_MIGRATION_TEST_PREFIX}{suffix}",
                "event_type": event_type,
                "origin_time": datetime(2026, 9, 25, 1, 0, tzinfo=UTC),
                "place": f"migration test {suffix}",
                "t1_at": datetime.fromisoformat(t1_at) if t1_at else None,
            }
            if lifecycle_state is _USE_DATABASE_DEFAULT:
                await connection.execute(
                    text(
                        """
                        INSERT INTO earthquake_events (
                            id,
                            source,
                            canonical_source_id,
                            event_type,
                            origin_time,
                            longitude,
                            latitude,
                            depth_km,
                            magnitude,
                            place,
                            geom,
                            t1_at
                        )
                        VALUES (
                            CAST(:id AS uuid),
                            :source,
                            :canonical_source_id,
                            :event_type,
                            :origin_time,
                            121.5,
                            31.2,
                            10,
                            4.5,
                            :place,
                            ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326),
                            :t1_at
                        )
                        """
                    ),
                    parameters,
                )
                return

            await connection.execute(
                text(
                    """
                    INSERT INTO earthquake_events (
                        id,
                        source,
                        canonical_source_id,
                        event_type,
                        origin_time,
                        longitude,
                        latitude,
                        depth_km,
                        magnitude,
                        place,
                        geom,
                        lifecycle_state,
                        t1_at
                    )
                    VALUES (
                        CAST(:id AS uuid),
                        :source,
                        :canonical_source_id,
                        :event_type,
                        :origin_time,
                        121.5,
                        31.2,
                        10,
                        4.5,
                        :place,
                        ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326),
                        :lifecycle_state,
                        :t1_at
                    )
                    """
                ),
                {**parameters, "lifecycle_state": lifecycle_state},
            )
    finally:
        await engine.dispose()


async def _non_cenc_lifecycle_migration_state() -> dict[str, object]:
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as connection:
            default = await connection.scalar(
                text(
                    """
                    SELECT column_default
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'earthquake_events'
                      AND column_name = 'lifecycle_state'
                    """
                )
            )
            rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT canonical_source_id, event_type, lifecycle_state, t1_at
                            FROM earthquake_events
                            WHERE canonical_source_id LIKE :prefix
                            ORDER BY canonical_source_id
                            """
                        ),
                        {"prefix": f"{NON_CENC_MIGRATION_TEST_PREFIX}%"},
                    )
                )
                .mappings()
                .all()
            )
        return {"default": str(default), "rows": [dict(row) for row in rows]}
    finally:
        await engine.dispose()


def _database_url() -> str:
    from app.config import settings

    return settings.database_url


def test_migration_identifiers_fit_alembic_version_column() -> None:
    oversized: list[str] = []

    for migration in MIGRATIONS_DIR.glob("*.py"):
        tree = ast.parse(migration.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            if node.target.id not in {"revision", "down_revision"}:
                continue

            value = ast.literal_eval(node.value)
            identifiers = value if isinstance(value, tuple) else (value,)
            for identifier in identifiers:
                if identifier is not None and len(identifier) > ALEMBIC_VERSION_LENGTH:
                    oversized.append(f"{migration.name}:{node.target.id}={identifier}")

    assert oversized == []


def test_migration_head_includes_assessment_orchestration() -> None:
    alembic_config = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))

    assert ScriptDirectory.from_config(alembic_config).get_current_head() == LATEST_REVISION


async def _assessment_orchestration_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    await engine.dispose()
    return {"assessment_runs", "assessment_tasks"} <= table_names


async def _intensity_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    await engine.dispose()
    return {
        "assessment_task_attempts",
        "intensity_field_products",
        "intensity_rasters",
    } <= names


async def _loss_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    return LOSS_TABLES <= names


async def _artifact_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    return ARTIFACT_TABLES <= names


async def _intensity_schema_state() -> dict[str, object]:
    tables = (
        "assessment_runs",
        "assessment_tasks",
        "earthquake_events",
        "assessment_task_attempts",
        "intensity_field_products",
        "intensity_rasters",
    )
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    table: {column["name"] for column in inspect(sync).get_columns(table)}
                    for table in tables
                    if table in inspect(sync).get_table_names()
                }
            )
            foreign_key_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                conrelid::regclass::text AS table_name,
                                pg_get_constraintdef(oid) AS definition
                            FROM pg_constraint
                            WHERE contype = 'f'
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
            unique_constraint_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                conname,
                                conrelid::regclass::text AS table_name,
                                pg_get_constraintdef(oid) AS definition
                            FROM pg_constraint
                            WHERE contype = 'u'
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
            index_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT indexname
                            FROM pg_indexes
                            WHERE schemaname = current_schema()
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )

        return {
            "columns": columns,
            "foreign_keys": {
                (row["table_name"], row["definition"])
                for row in foreign_key_rows
                if row["table_name"] in tables
            },
            "unique_constraints": {
                (row["table_name"], row["conname"], row["definition"])
                for row in unique_constraint_rows
                if row["table_name"] in tables
            },
            "indexes": {
                row["indexname"]
                for row in index_rows
            },
        }
    finally:
        await engine.dispose()


def _intensity_added_columns(state: dict[str, object]) -> set[tuple[str, str]]:
    columns = state["columns"]
    expected = {
        ("assessment_runs", "report_ingested_at"),
        ("assessment_runs", "deadline_basis_at"),
        ("assessment_runs", "deadline_exceeded_at"),
        ("assessment_runs", "duration_ms"),
        ("assessment_runs", "algorithm_bundle_version"),
        ("assessment_runs", "superseded_by_run_id"),
        ("assessment_runs", "superseded_at"),
        ("assessment_tasks", "input_fingerprint"),
        ("assessment_tasks", "output_checksum"),
        ("assessment_tasks", "algorithm_version"),
        ("earthquake_events", "latest_assessment_run_id"),
        ("earthquake_events", "effective_assessment_run_id"),
    }
    return {
        (table, column)
        for table, column in expected
        if column in columns.get(table, set())
    }


def _intensity_added_foreign_keys(state: dict[str, object]) -> set[tuple[str, str]]:
    expected = {
        (
            "assessment_runs",
            "FOREIGN KEY (superseded_by_run_id) REFERENCES assessment_runs(id) ON DELETE SET NULL",
        ),
        (
            "earthquake_events",
            "FOREIGN KEY (latest_assessment_run_id) REFERENCES assessment_runs(id) ON DELETE SET NULL",
        ),
        (
            "earthquake_events",
            "FOREIGN KEY (effective_assessment_run_id) REFERENCES assessment_runs(id) ON DELETE SET NULL",
        ),
        (
            "assessment_task_attempts",
            "FOREIGN KEY (task_id) REFERENCES assessment_tasks(id) ON DELETE CASCADE",
        ),
        (
            "intensity_field_products",
            "FOREIGN KEY (run_id) REFERENCES assessment_runs(id) ON DELETE CASCADE",
        ),
        (
            "intensity_field_products",
            "FOREIGN KEY (task_id) REFERENCES assessment_tasks(id) ON DELETE CASCADE",
        ),
        (
            "intensity_rasters",
            "FOREIGN KEY (product_id) REFERENCES intensity_field_products(id) ON DELETE CASCADE",
        ),
    }
    return state["foreign_keys"] & expected


def _intensity_added_unique_constraints(
    state: dict[str, object],
) -> set[tuple[str, str, str]]:
    expected = {
        (
            "assessment_task_attempts",
            "uq_assessment_task_attempts_number",
            "UNIQUE (task_id, attempt_number)",
        ),
        (
            "intensity_field_products",
            "uq_intensity_product_run_type",
            "UNIQUE (run_id, product_type)",
        ),
        (
            "intensity_rasters",
            "intensity_rasters_product_id_key",
            "UNIQUE (product_id)",
        ),
    }
    return state["unique_constraints"] & expected


def _intensity_added_indexes(state: dict[str, object]) -> set[str]:
    expected = {
        "ix_assessment_runs_report_ingested_at",
        "ix_assessment_runs_deadline_basis_at",
        "ix_assessment_runs_superseded_by_run_id",
        "ix_assessment_tasks_input_fingerprint",
        "ix_assessment_task_attempts_status",
        "ix_intensity_products_task_id",
        "ix_intensity_products_product_type",
        "ix_intensity_products_status",
        "ix_intensity_products_quality_grade",
        "ix_intensity_products_source_product_id",
        "ix_earthquake_events_latest_assessment_run_id",
        "ix_earthquake_events_effective_assessment_run_id",
    }
    return state["indexes"] & expected


async def test_0010_assessment_orchestration_is_reversible() -> None:
    _set_revision(NON_CENC_REVISION)
    assert await _assessment_orchestration_tables_exist() is False

    try:
        _set_revision(LATEST_REVISION)
        assert await _assessment_orchestration_tables_exist() is True

        _set_revision(NON_CENC_REVISION)
        assert await _assessment_orchestration_tables_exist() is False

        _set_revision(LATEST_REVISION)
        assert await _assessment_orchestration_tables_exist() is True
    finally:
        _set_revision(LATEST_REVISION)


async def test_0011_intensity_assessment_is_reversible() -> None:
    _set_revision(INTENSITY_PREVIOUS_REVISION)
    assert await _intensity_tables_exist() is False
    previous_state = await _intensity_schema_state()
    assert _intensity_added_columns(previous_state) == set()
    assert _intensity_added_foreign_keys(previous_state) == set()
    assert _intensity_added_unique_constraints(previous_state) == set()
    assert _intensity_added_indexes(previous_state) == set()
    try:
        _set_revision(LATEST_REVISION)
        assert await _intensity_tables_exist() is True
        head_state = await _intensity_schema_state()
        assert len(_intensity_added_columns(head_state)) == 12
        assert len(_intensity_added_foreign_keys(head_state)) == 7
        assert len(_intensity_added_unique_constraints(head_state)) == 3
        assert len(_intensity_added_indexes(head_state)) == 12
        _set_revision(INTENSITY_PREVIOUS_REVISION)
        assert await _intensity_tables_exist() is False
        downgraded_state = await _intensity_schema_state()
        assert _intensity_added_columns(downgraded_state) == set()
        assert _intensity_added_foreign_keys(downgraded_state) == set()
        assert _intensity_added_unique_constraints(downgraded_state) == set()
        assert _intensity_added_indexes(downgraded_state) == set()
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)


async def test_0008_backfills_old_rows_and_downgrade_upgrade_is_reversible() -> None:
    _set_revision(OLD_REGION_REVISION)
    await _delete_migration_test_rows()
    await _insert_old_region_boundary(
        source_uri="https://example.gov.invalid/regions/shanghai.geojson",
        checksum="a" * 64,
    )

    try:
        _set_revision(REGION_MARITIME_REVISION)
        upgraded = await _region_boundary_state()

        assert upgraded["columns"]["maritime_geom"]["nullable"] is False
        assert upgraded["columns"]["source_uri"]["nullable"] is False
        assert upgraded["columns"]["checksum"]["nullable"] is False
        assert upgraded["row"]["maritime_empty"] is True
        assert upgraded["row"]["administrative_covers"] is True
        assert upgraded["row"]["source_uri"].startswith("https://")
        assert upgraded["row"]["checksum"] == "a" * 64

        _set_revision(OLD_REGION_REVISION)
        downgraded = await _region_boundary_state(include_maritime=False)

        assert "maritime_geom" not in downgraded["columns"]
        assert downgraded["columns"]["source_uri"]["nullable"] is True
        assert downgraded["columns"]["checksum"]["nullable"] is True
        assert downgraded["row"]["administrative_covers"] is True

        _set_revision(REGION_MARITIME_REVISION)
        re_upgraded = await _region_boundary_state()

        assert re_upgraded["row"]["maritime_empty"] is True
        assert re_upgraded["row"]["source_uri"].startswith("https://")
        assert re_upgraded["row"]["checksum"] == "a" * 64
    finally:
        await _delete_migration_test_rows()
        _set_revision(LATEST_REVISION)


async def test_0008_fails_when_old_row_has_null_audit_data() -> None:
    _set_revision(OLD_REGION_REVISION)
    await _delete_migration_test_rows()
    await _insert_old_region_boundary(
        source_uri=None,
        checksum="b" * 64,
    )

    try:
        result = _alembic("upgrade", REGION_MARITIME_REVISION, check=False)

        assert result.returncode != 0
        assert "source_uri" in result.stderr or "source_uri" in result.stdout
        assert _current_revision() == OLD_REGION_REVISION
    finally:
        await _delete_migration_test_rows()
        _set_revision(LATEST_REVISION)


async def test_0009_backfills_non_cenc_events_and_downgrade_upgrade_is_reversible() -> None:
    _set_revision(REGION_MARITIME_REVISION)
    await _delete_non_cenc_migration_test_rows()
    for suffix, event_type in (
        ("manual", "manual"),
        ("test", "test"),
        ("drill", "drill"),
        ("auto", "auto"),
    ):
        await _insert_non_cenc_migration_test_event(
            suffix=suffix,
            event_type=event_type,
        )

    try:
        _set_revision(LATEST_REVISION)
        upgraded = await _non_cenc_lifecycle_migration_state()

        assert "not_applicable" in upgraded["default"]
        upgraded_rows = {
            row["canonical_source_id"]: row for row in upgraded["rows"]
        }
        for suffix in ("manual", "test", "drill"):
            row = upgraded_rows[f"{NON_CENC_MIGRATION_TEST_PREFIX}{suffix}"]
            assert row["lifecycle_state"] == "not_applicable"
            assert row["t1_at"] is None
        auto_row = upgraded_rows[f"{NON_CENC_MIGRATION_TEST_PREFIX}auto"]
        assert auto_row["lifecycle_state"] == "auto_pending"
        assert auto_row["t1_at"] is not None

        await _insert_non_cenc_migration_test_event(
            suffix="default",
            event_type="test",
            lifecycle_state=_USE_DATABASE_DEFAULT,
            t1_at=None,
        )
        defaulted = await _non_cenc_lifecycle_migration_state()
        default_row = next(
            row
            for row in defaulted["rows"]
            if row["canonical_source_id"]
            == f"{NON_CENC_MIGRATION_TEST_PREFIX}default"
        )
        assert default_row["lifecycle_state"] == "not_applicable"
        assert default_row["t1_at"] is None

        _set_revision(REGION_MARITIME_REVISION)
        downgraded = await _non_cenc_lifecycle_migration_state()

        assert "auto_pending" in downgraded["default"]
        downgraded_rows = {
            row["canonical_source_id"]: row for row in downgraded["rows"]
        }
        for suffix in ("manual", "test", "drill", "default"):
            assert (
                downgraded_rows[f"{NON_CENC_MIGRATION_TEST_PREFIX}{suffix}"][
                    "lifecycle_state"
                ]
                == "auto_pending"
            )

        _set_revision(LATEST_REVISION)
        re_upgraded = await _non_cenc_lifecycle_migration_state()
        assert "not_applicable" in re_upgraded["default"]
    finally:
        await _delete_non_cenc_migration_test_rows()
        _set_revision(LATEST_REVISION)


async def test_0014_loss_assessment_is_reversible() -> None:
    _set_revision(LOSS_PREVIOUS_REVISION)
    assert await _loss_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _loss_tables_exist() is True
        _set_revision(LOSS_PREVIOUS_REVISION)
        assert await _loss_tables_exist() is False
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)


async def test_0015_artifact_production_is_reversible() -> None:
    _set_revision(ARTIFACT_PREVIOUS_REVISION)
    assert await _artifact_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _artifact_tables_exist() is True
        _set_revision(ARTIFACT_PREVIOUS_REVISION)
        assert await _artifact_tables_exist() is False
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)
