from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import Integer, String, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import configure_mappers

import app.assessment.models  # noqa: F401
import app.data_assets.models  # noqa: F401
import app.events.models  # noqa: F401
import app.intensity.models  # noqa: F401
import app.loss.models  # noqa: F401
from app.assessment.models import AssessmentRun, AssessmentTask
from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    ArtifactTaskDependencyBinding,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionInputSnapshotItem,
    ProductionRun,
    ProductionTask,
)
from app.config import settings
from app.data_assets.models import DataAsset, DataAssetVersion
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.intensity.models import IntensityFieldProduct
from app.loss.models import LossProduct
from geoalchemy2.elements import WKTElement

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

ARTIFACT_UNIQUE_CONSTRAINTS = {
    "uq_artifact_template_key",
    "uq_artifact_template_version",
    "uq_artifact_run_generation",
    "uq_production_input_snapshot_run",
    "uq_production_input_snapshot_item",
    "uq_artifact_task_output",
    "uq_generated_artifact_version",
    "uq_artifact_override_request",
}

ARTIFACT_PARTIAL_UNIQUE_INDEXES = {
    "uq_artifact_run_current_scope",
    "uq_artifact_publication_current",
    "uq_artifact_dependency_product",
    "uq_artifact_dependency_artifact",
    "uq_generated_artifact_final_task",
}

ARTIFACT_CHECK_CONSTRAINTS = {
    "ck_artifact_template_version_status",
    "ck_artifact_run_status",
    "ck_artifact_production_mode",
    "ck_artifact_launch_mode",
    "ck_artifact_deadline_kind",
    "ck_artifact_task_status",
    "ck_artifact_dependency_kind",
    "ck_artifact_dependency_resolution_status",
    "ck_artifact_dependency_kind_profile",
    "ck_artifact_dependency_optional",
    "ck_artifact_dependency_bound_fields",
    "ck_artifact_dependency_terminal_detail",
    "ck_generated_artifact_status",
    "ck_generated_artifact_publication_mode",
    "ck_artifact_override_request_status",
}

ARTIFACT_JSONB_COLUMNS = {
    ("artifact_template_versions", "manifest"),
    ("production_input_snapshots", "manifest"),
    ("production_input_snapshot_items", "coverage"),
    ("artifact_production_runs", "required_outputs"),
    ("artifact_production_runs", "snapshot"),
    ("artifact_production_tasks", "depends_on"),
    ("artifact_production_tasks", "optional_depends_on"),
    ("artifact_production_tasks", "result"),
    ("artifact_task_dependency_bindings", "resolution_detail"),
    ("generated_artifacts", "marker"),
    ("generated_artifacts", "template_snapshot"),
    ("generated_artifacts", "data_snapshot"),
    ("generated_artifacts", "render_manifest"),
    ("artifact_override_requests", "response_body"),
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
            snapshot_item_columns = await connection.run_sync(
                lambda sync: {
                    column["name"]: column["nullable"]
                    for column in inspect(sync).get_columns(
                        "production_input_snapshot_items"
                    )
                }
            )
    finally:
        await engine.dispose()

    assert ARTIFACT_TABLES <= tables
    assert t1_column["nullable"] is True
    assert snapshot_item_columns["asset_version_id"] is True
    assert snapshot_item_columns["checksum"] is True


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
                            'uq_artifact_dependency_artifact',
                            'uq_generated_artifact_final_task'
                          )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert set(definitions) == ARTIFACT_PARTIAL_UNIQUE_INDEXES
    for definition in definitions.values():
        assert "CREATE UNIQUE INDEX" in definition
    assert "WHERE" in definitions["uq_artifact_run_current_scope"]
    assert "is_current" in definitions["uq_artifact_run_current_scope"]
    assert "superseded_at IS NULL" in definitions["uq_artifact_publication_current"]
    assert "dependency_kind = 'assessment_product'" in (
        definitions["uq_artifact_dependency_product"]
    )
    assert "dependency_kind = 'artifact'" in definitions["uq_artifact_dependency_artifact"]
    assert "is_final" in definitions["uq_generated_artifact_final_task"]


def test_artifact_orm_mappers_configure() -> None:
    assert ArtifactTemplate.__tablename__ == "artifact_templates"
    configure_mappers()


def test_artifact_orm_uses_planned_scalar_types() -> None:
    artifact_version_type = GeneratedArtifact.__table__.c.artifact_version.type
    assert isinstance(artifact_version_type, Integer)

    identity_columns = (
        ArtifactTemplateVersion.__table__.c.created_by,
        ArtifactPublication.__table__.c.published_by,
        ArtifactOverrideRequest.__table__.c.actor_id,
    )
    for column in identity_columns:
        assert isinstance(column.type, String)
        assert column.type.length == 64


async def test_artifact_schema_contains_full_plan_contract() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    table: {
                        column["name"]: column
                        for column in inspect(sync).get_columns(table)
                    }
                    for table in ARTIFACT_TABLES
                }
            )
            unique_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            constraint_record.conname,
                            pg_get_constraintdef(constraint_record.oid) AS definition
                        FROM pg_constraint AS constraint_record
                        JOIN pg_class AS target
                          ON target.oid = constraint_record.conrelid
                        JOIN pg_namespace AS target_schema
                          ON target_schema.oid = target.relnamespace
                        WHERE constraint_record.contype = 'u'
                          AND target_schema.nspname = current_schema()
                          AND target.relname = ANY(
                              CAST(:artifact_tables AS text[])
                          )
                        """
                    ),
                    {"artifact_tables": sorted(ARTIFACT_TABLES)},
                )
            ).mappings()
            check_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            constraint_record.conname,
                            pg_get_constraintdef(constraint_record.oid) AS definition
                        FROM pg_constraint AS constraint_record
                        JOIN pg_class AS target
                          ON target.oid = constraint_record.conrelid
                        JOIN pg_namespace AS target_schema
                          ON target_schema.oid = target.relnamespace
                        WHERE constraint_record.contype = 'c'
                          AND target_schema.nspname = current_schema()
                          AND target.relname = ANY(
                              CAST(:artifact_tables AS text[])
                          )
                        """
                    ),
                    {"artifact_tables": sorted(ARTIFACT_TABLES)},
                )
            ).mappings()
            default_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name, column_name, column_default
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = ANY(
                              CAST(:artifact_tables AS text[])
                          )
                        """
                    ),
                    {"artifact_tables": sorted(ARTIFACT_TABLES)},
                )
            ).mappings()
            foreign_key_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            constraint_record.conname,
                            pg_get_constraintdef(constraint_record.oid) AS definition
                        FROM pg_constraint AS constraint_record
                        JOIN pg_class AS target
                          ON target.oid = constraint_record.conrelid
                        JOIN pg_namespace AS target_schema
                          ON target_schema.oid = target.relnamespace
                        WHERE constraint_record.contype = 'f'
                          AND target_schema.nspname = current_schema()
                          AND target.relname = ANY(
                              CAST(:artifact_tables AS text[])
                          )
                        """
                    ),
                    {"artifact_tables": sorted(ARTIFACT_TABLES)},
                )
            ).mappings()
            jsonb_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND data_type = 'jsonb'
                        """
                    )
                )
            ).mappings()
    finally:
        await engine.dispose()

    unique_constraints = {
        row["conname"]: row["definition"]
        for row in unique_rows
        if row["conname"] in ARTIFACT_UNIQUE_CONSTRAINTS
    }
    assert set(unique_constraints) == ARTIFACT_UNIQUE_CONSTRAINTS
    assert "UNIQUE (template_key)" in unique_constraints["uq_artifact_template_key"]
    assert "UNIQUE (template_id, version)" in unique_constraints["uq_artifact_template_version"]
    assert "UNIQUE (assessment_run_id, generation_seq)" in unique_constraints[
        "uq_artifact_run_generation"
    ]
    assert "UNIQUE (production_run_id)" in unique_constraints[
        "uq_production_input_snapshot_run"
    ]
    assert "UNIQUE (snapshot_id, asset_key, role)" in unique_constraints[
        "uq_production_input_snapshot_item"
    ]
    assert "UNIQUE (production_run_id, artifact_key, output_profile)" in unique_constraints[
        "uq_artifact_task_output"
    ]
    assert "UNIQUE (event_id, artifact_key, output_profile, artifact_version)" in (
        unique_constraints["uq_generated_artifact_version"]
    )
    assert "UNIQUE (actor_id, endpoint, idempotency_key)" in unique_constraints[
        "uq_artifact_override_request"
    ]

    check_constraints = {
        row["conname"]: row["definition"]
        for row in check_rows
        if row["conname"] in ARTIFACT_CHECK_CONSTRAINTS
    }
    assert set(check_constraints) == ARTIFACT_CHECK_CONSTRAINTS
    assert "dependency_output_profile IS NULL" in check_constraints[
        "ck_artifact_dependency_kind_profile"
    ]
    assert "dependency_output_profile IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_kind_profile"
    ]
    assert "is_optional" in check_constraints["ck_artifact_dependency_optional"]
    assert "bound_entity_id IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "bound_version IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "bound_checksum IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "bound_entity_id IS NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "bound_version IS NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "bound_checksum IS NULL" in check_constraints[
        "ck_artifact_dependency_bound_fields"
    ]
    assert "resolution_detail IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_terminal_detail"
    ]
    assert "jsonb_typeof(resolution_detail)" in check_constraints[
        "ck_artifact_dependency_terminal_detail"
    ]
    assert "'{}'::jsonb" in check_constraints[
        "ck_artifact_dependency_terminal_detail"
    ]
    assert "resolved_at IS NOT NULL" in check_constraints[
        "ck_artifact_dependency_terminal_detail"
    ]

    defaults = {
        (row["table_name"], row["column_name"]): row["column_default"]
        for row in default_rows
        if row["table_name"] in ARTIFACT_TABLES
    }
    expected_default_fragments = {
        ("artifact_template_versions", "status"): "'draft'",
        ("production_input_snapshot_items", "selected_for_render"): "false",
        ("artifact_production_runs", "status"): "'pending'",
        ("artifact_production_runs", "priority"): "100",
        ("artifact_production_runs", "is_current"): "false",
        ("artifact_production_tasks", "status"): "'pending'",
        ("artifact_production_tasks", "priority"): "100",
        ("artifact_production_tasks", "depends_on"): "'[]'::jsonb",
        ("artifact_production_tasks", "optional_depends_on"): "'[]'::jsonb",
        ("artifact_production_tasks", "attempt_count"): "0",
        ("artifact_production_tasks", "max_attempts"): "3",
        ("artifact_task_dependency_bindings", "is_optional"): "false",
        ("generated_artifacts", "is_final"): "false",
        ("generated_artifacts", "needs_review"): "false",
        ("artifact_publications", "is_forced"): "false",
        ("artifact_override_requests", "status"): "'processing'",
        ("artifact_override_requests", "lease_generation"): "1",
        ("artifact_override_requests", "attempt_count"): "0",
    }
    for key, fragment in expected_default_fragments.items():
        assert fragment in defaults[key]

    assert isinstance(columns["generated_artifacts"]["artifact_version"]["type"], Integer)
    for table, column_name in (
        ("artifact_template_versions", "created_by"),
        ("artifact_publications", "published_by"),
        ("artifact_override_requests", "actor_id"),
    ):
        column_type = columns[table][column_name]["type"]
        assert isinstance(column_type, String)
        assert column_type.length == 64

    assert {
        (row["table_name"], row["column_name"]) for row in jsonb_rows
    } >= ARTIFACT_JSONB_COLUMNS

    superseded_fk = {
        row["conname"]: row["definition"]
        for row in foreign_key_rows
        if row["conname"] == "fk_generated_artifacts_superseded_by"
    }
    assert "FOREIGN KEY (superseded_by_id)" in (
        superseded_fk["fk_generated_artifacts_superseded_by"]
    )
    assert "REFERENCES generated_artifacts(id) ON DELETE SET NULL" in (
        superseded_fk["fk_generated_artifacts_superseded_by"]
    )


async def _create_run(
    session,
    *,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
    generation_seq: int,
) -> ProductionRun:
    now = datetime.now(UTC)
    run = ProductionRun(
        event_id=event_id,
        revision_id=revision_id,
        revision_no=1,
        production_mode="live",
        launch_mode="standalone",
        deadline_basis_at=now,
        deadline_at=now + timedelta(seconds=300),
        deadline_kind="rebuild_deadline",
        catalog_version="artifact-test-v1",
        generation_seq=generation_seq,
        generation_scope=f"contract:{generation_seq}",
        required_outputs=[],
    )
    session.add(run)
    await session.flush()
    return run


@asynccontextmanager
async def _rolled_back_session():
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _create_event_graph(session) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    raw_message = RawMessage(
        source="artifact-schema-test",
        message_kind="test",
        checksum=uuid.uuid4().hex + uuid.uuid4().hex,
        payload={},
    )
    event = EarthquakeEvent(
        source="artifact-schema-test",
        canonical_source_id=f"artifact-schema-{uuid.uuid4()}",
        event_type="test",
        origin_time=now,
        longitude=121.5,
        latitude=31.2,
        depth_km=10,
        magnitude=5.2,
        place="artifact schema test",
        geom=WKTElement("POINT(121.5 31.2)", srid=4326),
    )
    session.add_all([raw_message, event])
    await session.flush()
    revision = EarthquakeRevision(
        event_id=event.id,
        raw_message_id=raw_message.id,
        revision_no=1,
        revision_kind="test",
        origin_time=now,
        longitude=121.5,
        latitude=31.2,
        depth_km=10,
        magnitude=5.2,
        place="artifact schema test",
        is_current=True,
    )
    session.add(revision)
    await session.flush()
    event.current_revision_id = revision.id
    await session.flush()
    return event.id, revision.id


async def _create_task(
    session,
    *,
    run: ProductionRun,
) -> ProductionTask:
    task = ProductionTask(
        production_run_id=run.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        kind="map",
        sequence=1,
        deadline_at=run.deadline_at,
    )
    session.add(task)
    await session.flush()
    return task


async def _create_assessment_run(
    session,
    *,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> AssessmentRun:
    now = datetime.now(UTC)
    outbox = EventLifecycleOutbox(
        event_id=event_id,
        revision_id=revision_id,
        trigger_type=f"artifact-schema-{uuid.uuid4()}",
        trigger_reason="live",
        payload={},
        status="pending",
    )
    session.add(outbox)
    await session.flush()

    assessment_run = AssessmentRun(
        event_id=event_id,
        revision_id=revision_id,
        outbox_id=outbox.id,
        run_no=1,
        trigger_reason="live",
        deadline_at=now + timedelta(seconds=300),
        snapshot={},
        report_ingested_at=now,
        deadline_basis_at=now,
    )
    session.add(assessment_run)
    await session.flush()
    return assessment_run


async def _create_assessment_task(
    session,
    *,
    assessment_run: AssessmentRun,
) -> AssessmentTask:
    task = AssessmentTask(
        run_id=assessment_run.id,
        task_key="intensity.fusion",
        task_type="intensity",
        component="intensity",
        sequence=1,
        deadline_at=assessment_run.deadline_at,
    )
    session.add(task)
    await session.flush()
    return task


async def _create_intensity_product(
    session,
    *,
    assessment_run: AssessmentRun,
    assessment_task: AssessmentTask,
    version: str,
    checksum: str,
) -> IntensityFieldProduct:
    product = IntensityFieldProduct(
        run_id=assessment_run.id,
        task_id=assessment_task.id,
        product_type="fusion",
        status="succeeded",
        algorithm_version=version,
        parameter_version="parameter-v1",
        grid_definition_version="grid-v1",
        region_profile_version="region-v1",
        input_fingerprint="a" * 64,
        input_checksum="b" * 64,
        output_checksum=checksum,
        coverage_ratio=1,
        statistics={},
    )
    session.add(product)
    await session.flush()
    return product


async def _create_loss_product(
    session,
    *,
    assessment_run: AssessmentRun,
    assessment_task: AssessmentTask,
    product_type: str,
    version: str,
    checksum: str,
) -> LossProduct:
    product = LossProduct(
        run_id=assessment_run.id,
        task_id=assessment_task.id,
        product_type=product_type,
        status="complete",
        quality_grade="L1",
        calibration_status="calibrated",
        coverage_ratio=1,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=True,
        algorithm_version=version,
        parameter_version="parameter-v1",
        region_profile_version="region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum=checksum,
        statistics={},
    )
    session.add(product)
    await session.flush()
    return product


async def _create_artifact(
    session,
    *,
    run: ProductionRun,
    task: ProductionTask,
    event_id: uuid.UUID,
    revision_id: uuid.UUID,
    version: int,
    is_final: bool = False,
) -> GeneratedArtifact:
    artifact = GeneratedArtifact(
        production_run_id=run.id,
        production_task_id=task.id,
        event_id=event_id,
        revision_id=revision_id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        artifact_version=version,
        is_final=is_final,
        production_mode="live",
        status="complete",
        quality_grade="A",
        publication_mode="automatic",
        file_name=f"epicenter-v{version}.jpg",
        format="jpg",
        storage_path=f"objects/{version}/epicenter.jpg",
        checksum=f"{version:064d}"[-64:],
        size_bytes=1024,
        generated_at=datetime.now(UTC),
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def test_generated_artifact_integer_versions_are_writable() -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        first = await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=1,
        )
        second = await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=2,
            is_final=True,
        )

        assert first.artifact_version == 1
        assert second.artifact_version == 2


async def test_generated_artifact_final_task_index_is_unique() -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=1,
            is_final=True,
        )

        with pytest.raises(IntegrityError, match="uq_generated_artifact_final_task"):
            async with session.begin_nested():
                await _create_artifact(
                    session,
                    run=run,
                    task=task,
                    event_id=event_id,
                    revision_id=revision_id,
                    version=2,
                    is_final=True,
                )


async def test_production_input_snapshot_rejects_reverse_ownership_update() -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        first_run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        second_run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=2,
        )
        snapshot = ProductionInputSnapshot(
            production_run_id=first_run.id,
            context_fingerprint="e" * 64,
            region_id="shanghai",
            manifest={},
        )
        session.add(snapshot)
        await session.flush()

        with pytest.raises(IntegrityError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        UPDATE production_input_snapshots
                        SET production_run_id = :production_run_id
                        WHERE id = :snapshot_id
                        """
                    ),
                    {
                        "production_run_id": second_run.id,
                        "snapshot_id": snapshot.id,
                    },
                )


async def test_production_input_snapshot_identity_fields_are_immutable() -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        snapshot = ProductionInputSnapshot(
            production_run_id=run.id,
            context_fingerprint="f" * 64,
            region_id="shanghai",
            manifest={},
        )
        session.add(snapshot)
        await session.flush()

        for statement in (
            """
            UPDATE production_input_snapshots
            SET context_fingerprint = repeat('1', 64)
            WHERE id = :snapshot_id
            """,
            """
            UPDATE production_input_snapshots
            SET region_id = 'beijing'
            WHERE id = :snapshot_id
            """,
            """
            UPDATE production_input_snapshots
            SET manifest = '{"changed": true}'::jsonb
            WHERE id = :snapshot_id
            """,
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                async with session.begin_nested():
                    await session.execute(
                        text(statement),
                        {"snapshot_id": snapshot.id},
                    )


async def test_production_input_snapshot_items_are_immutable_and_cascadable() -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        asset = DataAsset(
            asset_key="artifact.snapshot.fixture",
            region_id="shanghai",
            name="Artifact snapshot fixture",
            data_type="vector",
            spatial_granularity="town",
            responsibility_unit="test",
            update_interval_days=1,
            is_core=False,
            contract={},
        )
        session.add(asset)
        await session.flush()
        asset_version = DataAssetVersion(
            asset_id=asset.id,
            version="1",
            source_uri="memory://artifact-snapshot-fixture",
            schema_summary={},
        )
        session.add(asset_version)
        await session.flush()
        snapshot = ProductionInputSnapshot(
            production_run_id=run.id,
            context_fingerprint="2" * 64,
            region_id="shanghai",
            manifest={},
        )
        session.add(snapshot)
        await session.flush()
        item = ProductionInputSnapshotItem(
            snapshot_id=snapshot.id,
            asset_key=asset.asset_key,
            asset_version_id=asset_version.id,
            checksum="3" * 64,
            role="render",
            coverage={},
            selected_for_render=True,
        )
        session.add(item)
        await session.flush()

        with pytest.raises(IntegrityError, match="immutable"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        UPDATE production_input_snapshot_items
                        SET selected_for_render = false
                        WHERE id = :item_id
                        """
                    ),
                    {"item_id": item.id},
                )

        await session.execute(
            text(
                """
                DELETE FROM production_input_snapshots
                WHERE id = :snapshot_id
                """
            ),
            {"snapshot_id": snapshot.id},
        )
        item_count = await session.scalar(
            text(
                """
                SELECT count(*)
                FROM production_input_snapshot_items
                WHERE id = :item_id
                """
            ),
            {"item_id": item.id},
        )
        assert item_count == 0


async def test_artifact_identity_contract_accepts_username_strings() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        artifact = await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=1,
            is_final=True,
        )
        template = ArtifactTemplate(
            template_key=f"map.epicenter.{uuid.uuid4()}",
            kind="map",
            display_name="Epicenter map",
        )
        session.add(template)
        await session.flush()
        template_version = ArtifactTemplateVersion(
            template_id=template.id,
            version="1",
            status="published",
            manifest={},
            checksum="a" * 64,
            storage_path="templates/map.epicenter/v1",
            created_by="superadmin",
        )
        publication = ArtifactPublication(
            event_id=event_id,
            revision_id=revision_id,
            revision_no=1,
            production_mode="live",
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            artifact_id=artifact.id,
            production_run_id=run.id,
            generation_seq=1,
            published_by="superadmin",
            published_at=now,
        )
        override = ArtifactOverrideRequest(
            actor_id="admin-id",
            endpoint="/api/v1/example",
            idempotency_key="00000000-0000-0000-0000-000000000001",
            request_fingerprint="b" * 64,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        session.add_all([template_version, publication, override])
        await session.flush()

        assert template_version.created_by == "superadmin"
        assert publication.published_by == "superadmin"
        assert override.actor_id == "admin-id"


async def test_production_run_rejects_snapshot_owned_by_another_run(
) -> None:
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        first_run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        second_run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=2,
        )
        snapshot = ProductionInputSnapshot(
            production_run_id=first_run.id,
            context_fingerprint="c" * 64,
            region_id="shanghai",
            manifest={},
        )
        session.add(snapshot)
        await session.flush()

        first_run.input_snapshot_id = snapshot.id
        await session.flush()

        with pytest.raises(IntegrityError, match="input_snapshot"):
            async with session.begin_nested():
                second_run.input_snapshot_id = snapshot.id
                await session.flush()


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "dependency_kind": "assessment_product",
            "dependency_output_profile": "summary",
        },
        {
            "dependency_kind": "artifact",
            "dependency_output_profile": None,
        },
        {
            "resolution_status": "omitted_after_wait",
            "is_optional": False,
            "resolution_detail": {"reason": "optional dependency unavailable"},
            "resolved_at": datetime.now(UTC),
        },
        {"bound_entity_id": None},
        {"bound_version": None},
        {"bound_checksum": None},
        {"resolution_status": "degraded", "bound_entity_id": None},
        {"resolution_status": "failed", "resolution_detail": None},
        {"resolution_status": "timed_out", "resolved_at": None},
        {"resolution_status": "canceled", "resolution_detail": None},
        {
            "resolution_status": "omitted_after_wait",
            "is_optional": True,
            "resolution_detail": None,
        },
        {
            "resolution_status": "omitted_after_wait",
            "is_optional": True,
            "resolved_at": None,
        },
        {
            "resolution_status": "failed",
            "bound_version": None,
            "resolution_detail": {"reason": "dependency failed"},
            "resolved_at": datetime.now(UTC),
        },
        {
            "resolution_status": "failed",
            "resolution_detail": {},
            "resolved_at": datetime.now(UTC),
        },
        {
            "resolution_status": "canceled",
            "resolution_detail": [],
            "resolved_at": datetime.now(UTC),
        },
    ),
)
async def test_dependency_binding_rejects_illegal_combinations(
    overrides: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        values: dict[str, object] = {
            "production_task_id": task.id,
            "dependency_kind": "artifact",
            "dependency_key": "map.intensity",
            "dependency_output_profile": "a3v-professional",
            "is_optional": False,
            "bound_entity_id": uuid.uuid4(),
            "bound_version": "1",
            "bound_checksum": "d" * 64,
            "resolution_status": "bound",
            "resolution_detail": {"source": "test"},
            "resolved_at": now,
        }
        values.update(overrides)

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(ArtifactTaskDependencyBinding(**values))
                await session.flush()


async def test_dependency_binding_accepts_real_assessment_product_references(
) -> None:
    now = datetime.now(UTC)
    assessment_dependencies = {
        "intensity.fusion": "intensity",
        "loss.buildings": "loss",
        "loss.population": "loss",
        "loss.casualties": "loss",
        "loss.economic": "loss",
        "loss.resources": "loss",
        "loss.validate": "loss",
    }
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        assessment_run = await _create_assessment_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
        )
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        run.assessment_run_id = assessment_run.id
        await session.flush()
        task = await _create_task(session, run=run)
        assessment_task = await _create_assessment_task(
            session,
            assessment_run=assessment_run,
        )

        bindings = []
        for index, (dependency_key, product_family) in enumerate(
            assessment_dependencies.items()
        ):
            version = f"{dependency_key}-v1"
            checksum = f"{index + 1:064x}"
            if product_family == "intensity":
                product_id = (
                    await _create_intensity_product(
                        session,
                        assessment_run=assessment_run,
                        assessment_task=assessment_task,
                        version=version,
                        checksum=checksum,
                    )
                ).id
            else:
                product_type = {
                    "loss.buildings": "building_damage",
                    "loss.population": "population_impact",
                    "loss.casualties": "casualties",
                    "loss.economic": "economic_loss",
                    "loss.resources": "resource_demand",
                    "loss.validate": "validation",
                }[dependency_key]
                product_id = (
                    await _create_loss_product(
                        session,
                        assessment_run=assessment_run,
                        assessment_task=assessment_task,
                        product_type=product_type,
                        version=version,
                        checksum=checksum,
                    )
                ).id
            bindings.append(
                ArtifactTaskDependencyBinding(
                    production_task_id=task.id,
                    dependency_kind="assessment_product",
                    dependency_key=dependency_key,
                    is_optional=False,
                    bound_entity_id=product_id,
                    bound_version=version,
                    bound_checksum=checksum,
                    resolution_status="bound",
                    resolution_detail={"source": "assessment"},
                    resolved_at=now,
                )
            )

        session.add_all(bindings)
        await session.flush()


async def test_dependency_binding_accepts_real_artifact_reference() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        artifact = await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=1,
        )
        binding = ArtifactTaskDependencyBinding(
            production_task_id=task.id,
            dependency_kind="artifact",
            dependency_key=artifact.artifact_key,
            dependency_output_profile=artifact.output_profile,
            is_optional=False,
            bound_entity_id=artifact.id,
            bound_version=str(artifact.artifact_version),
            bound_checksum=artifact.checksum,
            resolution_status="bound",
            resolution_detail={"source": "generated_artifact"},
            resolved_at=now,
        )
        session.add(binding)
        await session.flush()


async def test_dependency_binding_rejects_invalid_assessment_references() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        assessment_run = await _create_assessment_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
        )
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        run.assessment_run_id = assessment_run.id
        await session.flush()
        task = await _create_task(session, run=run)
        assessment_task = await _create_assessment_task(
            session,
            assessment_run=assessment_run,
        )
        product = await _create_intensity_product(
            session,
            assessment_run=assessment_run,
            assessment_task=assessment_task,
            version="fusion-v1",
            checksum="a" * 64,
        )
        base_values: dict[str, object] = {
            "production_task_id": task.id,
            "dependency_kind": "assessment_product",
            "dependency_key": "intensity.fusion",
            "is_optional": False,
            "bound_entity_id": product.id,
            "bound_version": product.algorithm_version,
            "bound_checksum": product.output_checksum,
            "resolution_status": "bound",
            "resolution_detail": {"source": "assessment"},
            "resolved_at": now,
        }

        for overrides in (
            {"bound_entity_id": uuid.uuid4()},
            {"bound_checksum": "b" * 64},
            {"bound_version": "fusion-v2"},
            {"dependency_key": "assessment.unsupported"},
        ):
            values = base_values | overrides
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(ArtifactTaskDependencyBinding(**values))
                    await session.flush()


async def test_dependency_binding_rejects_cross_run_assessment_product() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        first_event_id, first_revision_id = await _create_event_graph(session)
        first_assessment_run = await _create_assessment_run(
            session,
            event_id=first_event_id,
            revision_id=first_revision_id,
        )
        first_assessment_task = await _create_assessment_task(
            session,
            assessment_run=first_assessment_run,
        )
        product = await _create_intensity_product(
            session,
            assessment_run=first_assessment_run,
            assessment_task=first_assessment_task,
            version="fusion-v1",
            checksum="c" * 64,
        )

        second_event_id, second_revision_id = await _create_event_graph(session)
        second_assessment_run = await _create_assessment_run(
            session,
            event_id=second_event_id,
            revision_id=second_revision_id,
        )
        second_run = await _create_run(
            session,
            event_id=second_event_id,
            revision_id=second_revision_id,
            generation_seq=1,
        )
        second_run.assessment_run_id = second_assessment_run.id
        await session.flush()
        second_task = await _create_task(session, run=second_run)

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    ArtifactTaskDependencyBinding(
                        production_task_id=second_task.id,
                        dependency_kind="assessment_product",
                        dependency_key="intensity.fusion",
                        is_optional=False,
                        bound_entity_id=product.id,
                        bound_version=product.algorithm_version,
                        bound_checksum=product.output_checksum,
                        resolution_status="bound",
                        resolution_detail={"source": "assessment"},
                        resolved_at=now,
                    )
                )
                await session.flush()


async def test_dependency_binding_rejects_invalid_artifact_references() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        artifact = await _create_artifact(
            session,
            run=run,
            task=task,
            event_id=event_id,
            revision_id=revision_id,
            version=1,
        )
        base_values: dict[str, object] = {
            "production_task_id": task.id,
            "dependency_kind": "artifact",
            "dependency_key": artifact.artifact_key,
            "dependency_output_profile": artifact.output_profile,
            "is_optional": False,
            "bound_entity_id": artifact.id,
            "bound_version": str(artifact.artifact_version),
            "bound_checksum": artifact.checksum,
            "resolution_status": "bound",
            "resolution_detail": {"source": "generated_artifact"},
            "resolved_at": now,
        }

        for overrides in (
            {"bound_entity_id": uuid.uuid4()},
            {"bound_checksum": "e" * 64},
            {"bound_version": "2"},
            {"dependency_key": "map.other"},
            {"dependency_output_profile": "other-profile"},
        ):
            values = base_values | overrides
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(ArtifactTaskDependencyBinding(**values))
                    await session.flush()

        second_event_id, second_revision_id = await _create_event_graph(session)
        second_run = await _create_run(
            session,
            event_id=second_event_id,
            revision_id=second_revision_id,
            generation_seq=1,
        )
        second_task = await _create_task(session, run=second_run)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    ArtifactTaskDependencyBinding(
                        **(
                            base_values
                            | {
                                "production_task_id": second_task.id,
                                "dependency_key": artifact.artifact_key,
                                "dependency_output_profile": artifact.output_profile,
                            }
                        )
                    )
                )
                await session.flush()


async def test_dependency_binding_accepts_terminal_without_binding() -> None:
    now = datetime.now(UTC)
    async with _rolled_back_session() as session:
        event_id, revision_id = await _create_event_graph(session)
        run = await _create_run(
            session,
            event_id=event_id,
            revision_id=revision_id,
            generation_seq=1,
        )
        task = await _create_task(session, run=run)
        binding = ArtifactTaskDependencyBinding(
            production_task_id=task.id,
            dependency_kind="artifact",
            dependency_key="map.intensity",
            dependency_output_profile="a3v-professional",
            is_optional=True,
            resolution_status="omitted_after_wait",
            resolution_detail={"reason": "optional dependency unavailable"},
            resolved_at=now,
        )
        session.add(binding)
        await session.flush()
