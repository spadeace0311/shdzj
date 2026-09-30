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
from app.artifacts.models import (
    ArtifactOverrideRequest,
    ArtifactPublication,
    ArtifactTaskDependencyBinding,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.config import settings
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage
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
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE contype = 'u'
                        """
                    )
                )
            ).mappings()
            check_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE contype = 'c'
                        """
                    )
                )
            ).mappings()
            default_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name, column_name, column_default
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                        """
                    )
                )
            ).mappings()
            foreign_key_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE contype = 'f'
                        """
                    )
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
    assert "resolution_detail IS NOT NULL" in check_constraints[
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


async def test_dependency_binding_accepts_valid_terminal_combinations(
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
        session.add_all(
            [
                ArtifactTaskDependencyBinding(
                    production_task_id=task.id,
                    dependency_kind="assessment_product",
                    dependency_key="intensity.summary",
                    is_optional=False,
                    bound_entity_id=uuid.uuid4(),
                    bound_version="1",
                    bound_checksum="e" * 64,
                    resolution_status="bound",
                    resolution_detail={"source": "assessment"},
                    resolved_at=now,
                ),
                ArtifactTaskDependencyBinding(
                    production_task_id=task.id,
                    dependency_kind="artifact",
                    dependency_key="map.intensity",
                    dependency_output_profile="a3v-professional",
                    is_optional=True,
                    resolution_status="omitted_after_wait",
                    resolution_detail={"reason": "optional dependency unavailable"},
                    resolved_at=now,
                ),
            ]
        )
        await session.flush()
