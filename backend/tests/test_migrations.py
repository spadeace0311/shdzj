import ast
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

BACKEND_DIR = Path(__file__).parents[1]
MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations" / "versions"
ALEMBIC_VERSION_LENGTH = 32
LATEST_REVISION = "0022_cleanup_namespace"
CLEANUP_INTENT_REVISION = "0021_cleanup_intents"
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
    "artifact_production_cancel_requests",
}
COLLABORATION_TABLES = {
    "workgroup_definitions",
    "workgroup_memberships",
    "workgroup_roster_snapshots",
    "workgroup_attendance",
    "collaboration_settings",
    "collaboration_task_templates",
    "collaboration_task_template_versions",
    "collaboration_tasks",
    "collaboration_task_contributors",
    "collaboration_task_deliverables",
    "collaboration_deliverable_versions",
    "collaboration_deliverable_publications",
    "collaboration_task_events",
    "collaboration_notification_deliveries",
    "collaboration_projection_outbox",
    "command_hall_event_projections",
    "command_hall_group_projections",
    "command_hall_alert_projections",
    "event_purge_receipts",
}
CLEANUP_TABLES = {
    "event_object_cleanup_intents",
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
        table_names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await engine.dispose()
    return {"assessment_runs", "assessment_tasks"} <= table_names


async def _intensity_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
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
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    finally:
        await engine.dispose()
    return LOSS_TABLES <= names


async def _artifact_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    finally:
        await engine.dispose()
    return ARTIFACT_TABLES <= names


async def _collaboration_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    finally:
        await engine.dispose()
    return COLLABORATION_TABLES <= names


async def _cleanup_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    finally:
        await engine.dispose()
    return CLEANUP_TABLES <= names


async def _event_purge_receipt_schema_state() -> dict[str, object]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            if "event_purge_receipts" not in table_names:
                return {"exists": False}
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("event_purge_receipts")
                }
            )
            indexes = await connection.run_sync(
                lambda sync: {
                    index["name"]: bool(index["unique"])
                    for index in inspect(sync).get_indexes("event_purge_receipts")
                }
            )
    finally:
        await engine.dispose()
    return {
        "exists": True,
        "columns": columns,
        "indexes": indexes,
    }


async def _exercise_event_purge_receipt_identity() -> None:
    event_id = uuid.uuid4()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM event_purge_receipts
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
            insert_sql = text(
                """
                INSERT INTO event_purge_receipts (
                    id,
                    event_id,
                    idempotency_key,
                    actor,
                    deletion_counts,
                    storage_paths
                )
                VALUES (
                    :id,
                    :event_id,
                    :idempotency_key,
                    'migration-test',
                    '{}'::jsonb,
                    '[]'::jsonb
                )
                """
            )
            await connection.execute(
                insert_sql,
                {
                    "id": uuid.uuid4(),
                    "event_id": event_id,
                    "idempotency_key": "first-key",
                },
            )
            await connection.execute(
                insert_sql,
                {
                    "id": uuid.uuid4(),
                    "event_id": event_id,
                    "idempotency_key": "second-key",
                },
            )
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        insert_sql,
                        {
                            "id": uuid.uuid4(),
                            "event_id": event_id,
                            "idempotency_key": "first-key",
                        },
                    )
            await connection.execute(
                text(
                    """
                    DELETE FROM event_purge_receipts
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
    finally:
        await engine.dispose()


async def _artifact_override_event_schema_state() -> dict[str, object]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"]: column
                    for column in inspect(sync).get_columns("artifact_override_requests")
                }
            )
            foreign_keys = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys("artifact_override_requests")
            )
            indexes = await connection.run_sync(
                lambda sync: {
                    index["name"]
                    for index in inspect(sync).get_indexes("artifact_override_requests")
                }
            )
    finally:
        await engine.dispose()
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
    }


async def _insert_artifact_override_backfill_rows() -> tuple[uuid.UUID, uuid.UUID]:
    event_id = uuid.uuid4()
    unmatched_id = uuid.uuid4()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
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
                        lifecycle_state
                    )
                    VALUES (
                        :event_id,
                        'migration-0020',
                        :canonical_source_id,
                        'test',
                        now(),
                        121.5,
                        31.2,
                        10,
                        4.5,
                        'migration 0020 event',
                        ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326),
                        'active'
                    )
                    """
                ),
                {
                    "event_id": event_id,
                    "canonical_source_id": f"migration-0020-{event_id}",
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO artifact_override_requests (
                        id,
                        actor_id,
                        endpoint,
                        idempotency_key,
                        request_fingerprint,
                        status
                    )
                    VALUES (
                        :valid_id,
                        'migration-0020',
                        :valid_endpoint,
                        'valid-key',
                        :fingerprint,
                        'failed'
                    ),
                    (
                        :empty_id,
                        'migration-0020',
                        '',
                        'empty-key',
                        :fingerprint,
                        'failed'
                    ),
                    (
                        :unmatched_id,
                        'migration-0020',
                        :unmatched_endpoint,
                        'unmatched-key',
                        :fingerprint,
                        'failed'
                    )
                    """
                ),
                {
                    "valid_id": uuid.uuid4(),
                    "valid_endpoint": (
                        f"/api/v1/events/{event_id}/artifacts/map.epicenter/"
                        "override?output_profile=a3v-professional"
                    ),
                    "empty_id": uuid.uuid4(),
                    "unmatched_id": uuid.uuid4(),
                    "unmatched_endpoint": (
                        f"/api/v1/events/{unmatched_id}/artifacts/map.epicenter/"
                        "override?output_profile=a3v-professional"
                    ),
                    "fingerprint": "a" * 64,
                },
            )
    finally:
        await engine.dispose()
    return event_id, unmatched_id


async def _assert_artifact_override_event_backfill(event_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT idempotency_key, event_id
                        FROM artifact_override_requests
                        WHERE actor_id = 'migration-0020'
                        ORDER BY idempotency_key
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert {row["idempotency_key"]: row["event_id"] for row in rows} == {
            "empty-key": None,
            "unmatched-key": None,
            "valid-key": event_id,
        }
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO artifact_override_requests (
                            id,
                            actor_id,
                            endpoint,
                            idempotency_key,
                            request_fingerprint,
                            status,
                            event_id
                        )
                        VALUES (
                            :id,
                            'migration-0020-invalid',
                            '/api/v1/events/not-a-uuid/artifacts/x/override',
                            'invalid-fk',
                            :fingerprint,
                            'failed',
                            :missing_event_id
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "fingerprint": "b" * 64,
                        "missing_event_id": uuid.uuid4(),
                    },
                )
    finally:
        await engine.dispose()


async def _delete_artifact_override_backfill_rows(event_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM artifact_override_requests
                    WHERE actor_id LIKE 'migration-0020%'
                    """
                )
            )
            await connection.execute(
                text("DELETE FROM earthquake_events WHERE id = :event_id"),
                {"event_id": event_id},
            )
    finally:
        await engine.dispose()


async def _production_snapshot_item_identity_nullability() -> dict[str, bool]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"]: column["nullable"]
                    for column in inspect(sync).get_columns("production_input_snapshot_items")
                }
            )
    finally:
        await engine.dispose()
    return {
        "asset_version_id": columns["asset_version_id"],
        "checksum": columns["checksum"],
    }


async def _create_missing_snapshot_item_fixture() -> tuple[uuid.UUID, uuid.UUID]:
    from datetime import UTC, datetime, timedelta

    from geoalchemy2.elements import WKTElement

    from app.artifacts.models import (
        ProductionInputSnapshot,
        ProductionInputSnapshotItem,
        ProductionRun,
    )
    import app.data_assets.models  # noqa: F401
    from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    event_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            async with session.begin():
                raw = RawMessage(
                    id=raw_id,
                    source="migration-0016",
                    source_message_id=f"migration-0016-{event_id}",
                    message_kind="test",
                    checksum=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
                    payload={"fixture": "missing-snapshot-item"},
                    received_at=now,
                )
                event = EarthquakeEvent(
                    id=event_id,
                    source="migration-0016",
                    canonical_source_id=f"migration-0016-{event_id}",
                    event_type="test",
                    origin_time=now,
                    longitude=121.500000,
                    latitude=31.200000,
                    depth_km=10.00,
                    magnitude=5.2,
                    place="migration 0016 fixture",
                    geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                    lifecycle_state="active",
                )
                session.add_all([raw, event])
                await session.flush()
                revision = EarthquakeRevision(
                    event_id=event.id,
                    raw_message_id=raw.id,
                    revision_no=1,
                    revision_kind="test",
                    origin_time=event.origin_time,
                    longitude=event.longitude,
                    latitude=event.latitude,
                    depth_km=event.depth_km,
                    magnitude=event.magnitude,
                    place=event.place,
                    is_current=True,
                )
                session.add(revision)
                await session.flush()
                event.current_revision_id = revision.id
                run = ProductionRun(
                    event_id=event.id,
                    revision_id=revision.id,
                    revision_no=1,
                    production_mode="test",
                    launch_mode="standalone",
                    deadline_basis_at=now,
                    deadline_at=now + timedelta(seconds=300),
                    deadline_kind="rebuild_deadline",
                    catalog_version="migration-test-v1",
                    generation_seq=1,
                    generation_scope="fixture",
                    required_outputs=[],
                    is_current=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                await session.flush()
                snapshot = ProductionInputSnapshot(
                    production_run_id=run.id,
                    context_fingerprint="e" * 64,
                    region_id="shanghai",
                    manifest={"fixture": "missing-snapshot-item"},
                )
                session.add(snapshot)
                await session.flush()
                session.add(
                    ProductionInputSnapshotItem(
                        snapshot_id=snapshot.id,
                        asset_key="migration.missing",
                        asset_version_id=None,
                        checksum=None,
                        role="optional",
                        coverage={},
                        selected_for_render=False,
                    )
                )
                await session.flush()
        return event_id, raw_id
    finally:
        await engine.dispose()


async def _delete_missing_snapshot_item_fixture(
    event_id: uuid.UUID,
    raw_id: uuid.UUID,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM earthquake_events WHERE id = :event_id"),
                {"event_id": event_id},
            )
            await connection.execute(
                text("DELETE FROM raw_messages WHERE id = :raw_id"),
                {"raw_id": raw_id},
            )
    finally:
        await engine.dispose()


async def _missing_snapshot_item_state() -> dict[str, object]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            main_count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM production_input_snapshot_items
                    WHERE asset_key = 'migration.missing'
                    """
                )
            )
            archive_count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM production_input_snapshot_items_missing
                    WHERE asset_key = 'migration.missing'
                    """
                )
            )
            identity = (
                await connection.execute(
                    text(
                        """
                        SELECT asset_version_id, checksum
                        FROM production_input_snapshot_items
                        WHERE asset_key = 'migration.missing'
                        LIMIT 1
                        """
                    )
                )
            ).one_or_none()
    finally:
        await engine.dispose()
    return {
        "main_count": main_count,
        "archive_count": archive_count,
        "identity": ((identity[0], identity[1]) if identity is not None else None),
    }


async def _archive_identity_constraint_exists() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_constraint AS constraint_record
                        WHERE constraint_record.conname =
                              'uq_production_input_snapshot_items_missing_snapshot_asset_role'
                          AND constraint_record.conrelid =
                              'production_input_snapshot_items_missing'::regclass
                    )
                    """
                )
            )
    finally:
        await engine.dispose()
    return bool(exists)


async def _artifact_trigger_state() -> tuple[set[str], set[str]]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            trigger_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT trigger_record.tgname
                            FROM pg_trigger AS trigger_record
                            JOIN pg_class AS target
                              ON target.oid = trigger_record.tgrelid
                            JOIN pg_namespace AS target_schema
                              ON target_schema.oid = target.relnamespace
                            WHERE NOT trigger_record.tgisinternal
                              AND target_schema.nspname = current_schema()
                              AND target.relname = ANY(
                                  CAST(:artifact_tables AS text[])
                              )
                            """
                        ),
                        {"artifact_tables": sorted(ARTIFACT_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
            function_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT routine_record.proname
                            FROM pg_proc AS routine_record
                            JOIN pg_namespace AS routine_schema
                              ON routine_schema.oid = routine_record.pronamespace
                            WHERE routine_schema.nspname = current_schema()
                              AND routine_record.proname = ANY(
                                  CAST(:function_names AS text[])
                              )
                            """
                        ),
                        {
                            "function_names": [
                                "enforce_artifact_production_run_input_snapshot",
                                "enforce_artifact_production_snapshot_immutable",
                                "enforce_artifact_production_snapshot_item_immutable",
                                "enforce_artifact_task_dependency_reference",
                            ]
                        },
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()
    return (
        {row["tgname"] for row in trigger_rows},
        {row["proname"] for row in function_rows},
    )


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
                                target.relname AS table_name,
                                pg_get_constraintdef(constraint_record.oid) AS definition
                            FROM pg_constraint AS constraint_record
                            JOIN pg_class AS target
                              ON target.oid = constraint_record.conrelid
                            JOIN pg_namespace AS target_schema
                              ON target_schema.oid = target.relnamespace
                            WHERE constraint_record.contype = 'f'
                              AND target_schema.nspname = current_schema()
                              AND target.relname = ANY(
                                  CAST(:tables AS text[])
                              )
                            """,
                        ),
                        {"tables": list(tables)},
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
                                constraint_record.conname,
                                target.relname AS table_name,
                                pg_get_constraintdef(constraint_record.oid) AS definition
                            FROM pg_constraint AS constraint_record
                            JOIN pg_class AS target
                              ON target.oid = constraint_record.conrelid
                            JOIN pg_namespace AS target_schema
                              ON target_schema.oid = target.relnamespace
                            WHERE constraint_record.contype = 'u'
                              AND target_schema.nspname = current_schema()
                              AND target.relname = ANY(
                                  CAST(:tables AS text[])
                              )
                            """,
                        ),
                        {"tables": list(tables)},
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
            "indexes": {row["indexname"] for row in index_rows},
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
    return {(table, column) for table, column in expected if column in columns.get(table, set())}


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
        upgraded_rows = {row["canonical_source_id"]: row for row in upgraded["rows"]}
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
            if row["canonical_source_id"] == f"{NON_CENC_MIGRATION_TEST_PREFIX}default"
        )
        assert default_row["lifecycle_state"] == "not_applicable"
        assert default_row["t1_at"] is None

        _set_revision(REGION_MARITIME_REVISION)
        downgraded = await _non_cenc_lifecycle_migration_state()

        assert "auto_pending" in downgraded["default"]
        downgraded_rows = {row["canonical_source_id"]: row for row in downgraded["rows"]}
        for suffix in ("manual", "test", "drill", "default"):
            assert (
                downgraded_rows[f"{NON_CENC_MIGRATION_TEST_PREFIX}{suffix}"]["lifecycle_state"]
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
    expected_triggers = {
        "trg_artifact_production_runs_input_snapshot_match",
        "trg_production_input_snapshots_immutable",
        "trg_production_input_snapshot_items_immutable",
        "trg_artifact_task_dependency_binding_reference",
    }
    expected_functions = {
        "enforce_artifact_production_run_input_snapshot",
        "enforce_artifact_production_snapshot_immutable",
        "enforce_artifact_production_snapshot_item_immutable",
        "enforce_artifact_task_dependency_reference",
    }
    _set_revision(ARTIFACT_PREVIOUS_REVISION)
    assert await _artifact_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _artifact_tables_exist() is True
        triggers, functions = await _artifact_trigger_state()
        assert expected_triggers <= triggers
        assert expected_functions <= functions
        _set_revision(ARTIFACT_PREVIOUS_REVISION)
        assert await _artifact_tables_exist() is False
        triggers, functions = await _artifact_trigger_state()
        assert triggers.isdisjoint(expected_triggers)
        assert functions.isdisjoint(expected_functions)
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)


async def test_0016_production_snapshot_item_nullable_identity_is_reversible() -> None:
    previous_revision = "0015_artifact_production"
    _set_revision(previous_revision)
    before = await _production_snapshot_item_identity_nullability()
    assert before == {
        "asset_version_id": False,
        "checksum": False,
    }
    try:
        _set_revision(LATEST_REVISION)
        upgraded = await _production_snapshot_item_identity_nullability()
        assert upgraded == {
            "asset_version_id": True,
            "checksum": True,
        }
        _set_revision(previous_revision)
        downgraded = await _production_snapshot_item_identity_nullability()
        assert downgraded == {
            "asset_version_id": False,
            "checksum": False,
        }
    finally:
        _set_revision(LATEST_REVISION)


async def test_0016_preserves_missing_snapshot_items_across_downgrade_upgrade() -> None:
    _set_revision(LATEST_REVISION)
    event_id, raw_id = await _create_missing_snapshot_item_fixture()
    try:
        before = await _missing_snapshot_item_state()
        assert before["main_count"] == 1
        assert before["archive_count"] == 0

        _set_revision("0015_artifact_production")
        downgraded = await _missing_snapshot_item_state()
        assert downgraded["main_count"] == 0
        assert downgraded["archive_count"] == 1
        assert await _production_snapshot_item_identity_nullability() == {
            "asset_version_id": False,
            "checksum": False,
        }

        _set_revision(LATEST_REVISION)
        upgraded = await _missing_snapshot_item_state()
        assert upgraded["main_count"] == 1
        assert upgraded["archive_count"] == 0
        assert upgraded["identity"] == (None, None)
    finally:
        await _delete_missing_snapshot_item_fixture(event_id, raw_id)
        _set_revision(LATEST_REVISION)


async def test_0016_archive_has_unique_identity_contract() -> None:
    _set_revision(LATEST_REVISION)
    assert await _archive_identity_constraint_exists() is True


async def test_0016_clears_archive_orphans_after_deep_downgrade() -> None:
    _set_revision(LATEST_REVISION)
    event_id, raw_id = await _create_missing_snapshot_item_fixture()
    try:
        _set_revision("0015_artifact_production")
        downgraded = await _missing_snapshot_item_state()
        assert downgraded["main_count"] == 0
        assert downgraded["archive_count"] == 1

        _set_revision("0014_loss_assessment")
        _set_revision(LATEST_REVISION)
        restored = await _missing_snapshot_item_state()
        assert restored["main_count"] == 0
        assert restored["archive_count"] == 0
    finally:
        await _delete_missing_snapshot_item_fixture(event_id, raw_id)
        try:
            _set_revision(LATEST_REVISION)
        except AssertionError:
            pass


async def test_0016_artifact_retention_uses_application_only_state() -> None:
    from app.artifacts.retention import ArtifactRetentionService

    service = ArtifactRetentionService()
    assert service.retain_expired is not None


async def test_0018_collaboration_command_hall_is_reversible() -> None:
    previous_revision = "0017_production_cancel_outbox"
    _set_revision(previous_revision)
    assert await _collaboration_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _collaboration_tables_exist() is True
    finally:
        _set_revision(LATEST_REVISION)


async def test_0019_event_purge_receipts_are_reversible() -> None:
    previous_revision = "0018_collaboration_command_hall"
    _set_revision(previous_revision)
    assert (await _event_purge_receipt_schema_state())["exists"] is False
    try:
        _set_revision(LATEST_REVISION)
        upgraded = await _event_purge_receipt_schema_state()
        assert upgraded["exists"] is True
        assert upgraded["columns"] == {
            "id",
            "event_id",
            "idempotency_key",
            "actor",
            "deletion_counts",
            "storage_paths",
            "storage_namespace",
            "purged_at",
        }
        assert upgraded["indexes"] == {
            "uq_event_purge_receipt_event_key": True,
            "ix_event_purge_receipts_purged_at": False,
        }
        await _exercise_event_purge_receipt_identity()

        _set_revision(previous_revision)
        assert (await _event_purge_receipt_schema_state())["exists"] is False

        _set_revision(LATEST_REVISION)
        assert (await _event_purge_receipt_schema_state())["exists"] is True
    finally:
        _set_revision(LATEST_REVISION)


async def test_0020_artifact_override_event_identity_backfill_and_fk() -> None:
    previous_revision = "0019_event_purge_receipts"
    _set_revision(previous_revision)
    event_id, _unmatched_id = await _insert_artifact_override_backfill_rows()
    try:
        previous_state = await _artifact_override_event_schema_state()
        assert "event_id" not in previous_state["columns"]

        _set_revision(LATEST_REVISION)
        await _assert_artifact_override_event_backfill(event_id)
        state = await _artifact_override_event_schema_state()
        assert state["columns"]["event_id"]["nullable"] is True
        assert any(
            foreign_key["constrained_columns"] == ["event_id"]
            and foreign_key["referred_table"] == "earthquake_events"
            for foreign_key in state["foreign_keys"]
        )
        assert "ix_artifact_override_requests_event_id" in state["indexes"]

        _set_revision(previous_revision)
        downgraded = await _artifact_override_event_schema_state()
        assert "event_id" not in downgraded["columns"]

        _set_revision(LATEST_REVISION)
        re_upgraded = await _artifact_override_event_schema_state()
        assert "event_id" in re_upgraded["columns"]
    finally:
        _set_revision(LATEST_REVISION)
        await _delete_artifact_override_backfill_rows(event_id)


async def _cleanup_intent_schema_state() -> dict[str, object]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            if "event_object_cleanup_intents" not in table_names:
                return {"exists": False}
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("event_object_cleanup_intents")
                }
            )
            indexes = await connection.run_sync(
                lambda sync: {
                    index["name"]: bool(index["unique"])
                    for index in inspect(sync).get_indexes("event_object_cleanup_intents")
                }
            )
            checks = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_check_constraints(
                        "event_object_cleanup_intents"
                    )
                }
            )
            unique_constraints = await connection.run_sync(
                lambda sync: {
                    constraint["name"]
                    for constraint in inspect(sync).get_unique_constraints(
                        "event_object_cleanup_intents"
                    )
                }
            )
    finally:
        await engine.dispose()
    return {
        "exists": True,
        "columns": columns,
        "indexes": indexes,
        "checks": checks,
        "unique_constraints": unique_constraints,
    }


async def test_0021_cleanup_intents_are_reversible() -> None:
    previous_revision = "0020_override_event"
    _set_revision(previous_revision)
    assert await _cleanup_tables_exist() is False
    try:
        _set_revision(CLEANUP_INTENT_REVISION)
        state = await _cleanup_intent_schema_state()
        assert state["exists"] is True
        assert state["columns"] == {
            "id",
            "event_id",
            "source_kind",
            "source_key",
            "storage_path",
            "status",
            "attempt_count",
            "last_error",
            "lease_owner",
            "lease_expires_at",
            "created_at",
            "updated_at",
            "completed_at",
        }
        assert state["unique_constraints"] == {"uq_event_object_cleanup_source_path"}
        assert state["indexes"] == {
            "ix_event_object_cleanup_event_id": False,
            "ix_event_object_cleanup_claim": False,
            "uq_event_object_cleanup_source_path": True,
        }
        assert "ck_event_object_cleanup_status" in state["checks"]

        _set_revision(previous_revision)
        assert await _cleanup_tables_exist() is False

        _set_revision(CLEANUP_INTENT_REVISION)
        assert await _cleanup_tables_exist() is True
    finally:
        _set_revision(LATEST_REVISION)


async def test_0022_cleanup_namespace_backfills_safely_and_is_reversible() -> None:
    legacy_id = uuid.uuid4()
    _set_revision(CLEANUP_INTENT_REVISION)
    try:
        legacy_receipt_state = await _event_purge_receipt_schema_state()
        assert "storage_namespace" not in legacy_receipt_state["columns"]
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO event_object_cleanup_intents (
                            id,
                            event_id,
                            source_kind,
                            source_key,
                            storage_path,
                            status,
                            attempt_count
                        ) VALUES (
                            :id,
                            :event_id,
                            'retention',
                            :source_key,
                            'objects/aa/bb/legacy.txt',
                            'pending',
                            0
                        )
                        """
                    ),
                    {
                        "id": legacy_id,
                        "event_id": uuid.uuid4(),
                        "source_key": uuid.uuid4().hex,
                    },
                )
        finally:
            await engine.dispose()

        _set_revision(LATEST_REVISION)
        state = await _cleanup_intent_schema_state()
        assert state["exists"] is True
        assert state["columns"] == {
            "id",
            "event_id",
            "source_kind",
            "source_key",
            "storage_path",
            "storage_namespace",
            "max_attempts",
            "status",
            "attempt_count",
            "last_error",
            "lease_owner",
            "lease_expires_at",
            "created_at",
            "updated_at",
            "completed_at",
        }
        assert state["unique_constraints"] == {"uq_event_object_cleanup_namespace_source_path"}
        assert state["indexes"] == {
            "ix_event_object_cleanup_event_id": False,
            "ix_event_object_cleanup_claim": False,
            "uq_event_object_cleanup_namespace_source_path": True,
        }
        assert "ck_event_object_cleanup_status" in state["checks"]

        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT storage_namespace, max_attempts, status, last_error
                            FROM event_object_cleanup_intents
                            WHERE id = :id
                            """
                        ),
                        {"id": legacy_id},
                    )
                ).one()
        finally:
            await engine.dispose()
        assert row.storage_namespace is None
        assert row.max_attempts == 5
        assert row.status == "unprocessable"
        assert "manual remediation required" in row.last_error

        _set_revision(CLEANUP_INTENT_REVISION)
        state = await _cleanup_intent_schema_state()
        assert "storage_namespace" not in state["columns"]
        assert "max_attempts" not in state["columns"]
        downgraded_receipt_state = await _event_purge_receipt_schema_state()
        assert "storage_namespace" not in downgraded_receipt_state["columns"]

        _set_revision(LATEST_REVISION)
        state = await _cleanup_intent_schema_state()
        assert "storage_namespace" in state["columns"]
        assert "max_attempts" in state["columns"]
        upgraded_receipt_state = await _event_purge_receipt_schema_state()
        assert "storage_namespace" in upgraded_receipt_state["columns"]
    finally:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM event_object_cleanup_intents WHERE id = :id"),
                    {"id": legacy_id},
                )
        finally:
            await engine.dispose()
        _set_revision(LATEST_REVISION)
