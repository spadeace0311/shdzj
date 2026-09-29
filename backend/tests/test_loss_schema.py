import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import settings


def _new_id() -> str:
    return str(uuid.uuid4())


async def _insert_loss_product(connection: AsyncConnection) -> str:
    raw_message_id = _new_id()
    event_id = _new_id()
    revision_id = _new_id()
    outbox_id = _new_id()
    run_id = _new_id()
    task_id = _new_id()
    product_id = _new_id()
    origin_time = datetime(2026, 1, 1, tzinfo=UTC)

    await connection.execute(
        text(
            """
            INSERT INTO raw_messages (
                id, source, message_kind, checksum, payload
            )
            VALUES (
                CAST(:id AS uuid),
                :source,
                :message_kind,
                :checksum,
                '{}'::jsonb
            )
            """
        ),
        {
            "id": raw_message_id,
            "source": "loss-schema-test",
            "message_kind": "test",
            "checksum": f"raw-{raw_message_id}",
        },
    )
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
                geom
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
                ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326)
            )
            """
        ),
        {
            "id": event_id,
            "source": "loss-schema-test",
            "canonical_source_id": f"loss-schema-{event_id}",
            "event_type": "test",
            "origin_time": origin_time,
            "place": "loss schema test",
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO earthquake_revisions (
                id,
                event_id,
                raw_message_id,
                revision_no,
                revision_kind,
                origin_time,
                longitude,
                latitude,
                depth_km,
                magnitude,
                place
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:event_id AS uuid),
                CAST(:raw_message_id AS uuid),
                :revision_no,
                :revision_kind,
                :origin_time,
                :longitude,
                :latitude,
                :depth_km,
                :magnitude,
                :place
            )
            """
        ),
        {
            "id": revision_id,
            "event_id": event_id,
            "raw_message_id": raw_message_id,
            "revision_no": 1,
            "revision_kind": "test",
            "origin_time": origin_time,
            "longitude": 121.5,
            "latitude": 31.2,
            "depth_km": 10,
            "magnitude": 4.5,
            "place": "loss schema test",
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO event_lifecycle_outbox (
                id,
                event_id,
                revision_id,
                trigger_type,
                trigger_reason,
                payload,
                status
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:event_id AS uuid),
                CAST(:revision_id AS uuid),
                :trigger_type,
                :trigger_reason,
                '{}'::jsonb,
                :status
            )
            """
        ),
        {
            "id": outbox_id,
            "event_id": event_id,
            "revision_id": revision_id,
            "trigger_type": "assessment.requested",
            "trigger_reason": "loss-schema-test",
            "status": "pending",
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO assessment_runs (
                id,
                event_id,
                revision_id,
                outbox_id,
                run_no,
                trigger_reason,
                t1_at,
                deadline_at,
                snapshot,
                report_ingested_at,
                deadline_basis_at
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:event_id AS uuid),
                CAST(:revision_id AS uuid),
                CAST(:outbox_id AS uuid),
                :run_no,
                :trigger_reason,
                :t1_at,
                :deadline_at,
                '{}'::jsonb,
                :report_ingested_at,
                :deadline_basis_at
            )
            """
        ),
        {
            "id": run_id,
            "event_id": event_id,
            "revision_id": revision_id,
            "outbox_id": outbox_id,
            "run_no": 1,
            "trigger_reason": "loss-schema-test",
            "t1_at": origin_time,
            "deadline_at": origin_time,
            "report_ingested_at": origin_time,
            "deadline_basis_at": origin_time,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO assessment_tasks (
                id,
                run_id,
                task_key,
                task_type,
                component,
                sequence,
                deadline_at
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:run_id AS uuid),
                :task_key,
                :task_type,
                :component,
                :sequence,
                :deadline_at
            )
            """
        ),
        {
            "id": task_id,
            "run_id": run_id,
            "task_key": "loss.schema",
            "task_type": "test",
            "component": "loss",
            "sequence": 1,
            "deadline_at": origin_time,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO loss_products (
                id,
                run_id,
                task_id,
                product_type,
                status,
                quality_grade,
                calibration_status,
                coverage_ratio,
                partial_scope,
                needs_review,
                spatialized_estimate,
                algorithm_version,
                parameter_version,
                region_profile_version,
                input_fingerprint,
                input_checksum,
                output_checksum,
                statistics
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:run_id AS uuid),
                CAST(:task_id AS uuid),
                :product_type,
                :status,
                :quality_grade,
                :calibration_status,
                :coverage_ratio,
                :partial_scope,
                :needs_review,
                :spatialized_estimate,
                :algorithm_version,
                :parameter_version,
                :region_profile_version,
                :input_fingerprint,
                :input_checksum,
                :output_checksum,
                '{}'::jsonb
            )
            """
        ),
        {
            "id": product_id,
            "run_id": run_id,
            "task_id": task_id,
            "product_type": "building_damage",
            "status": "complete",
            "quality_grade": "L2",
            "calibration_status": "reference_uncalibrated",
            "coverage_ratio": 1,
            "partial_scope": False,
            "needs_review": False,
            "spatialized_estimate": False,
            "algorithm_version": "loss-schema-v1",
            "parameter_version": "loss-schema-parameters-v1",
            "region_profile_version": "loss-schema-region-v1",
            "input_fingerprint": "a" * 64,
            "input_checksum": "b" * 64,
            "output_checksum": "c" * 64,
        },
    )
    return product_id


async def _insert_metric(
    connection: AsyncConnection,
    *,
    product_id: str,
    value_status: str,
    numeric_value: float | None,
    area_code: str,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO loss_metric_values (
                id,
                product_id,
                area_scope,
                area_code,
                area_name,
                metric_key,
                value_type,
                value_status,
                numeric_value,
                unit,
                quality_grade
            )
            VALUES (
                CAST(:id AS uuid),
                CAST(:product_id AS uuid),
                :area_scope,
                :area_code,
                :area_name,
                :metric_key,
                :value_type,
                :value_status,
                :numeric_value,
                :unit,
                :quality_grade
            )
            """
        ),
        {
            "id": _new_id(),
            "product_id": product_id,
            "area_scope": "town",
            "area_code": area_code,
            "area_name": "loss schema test town",
            "metric_key": "affected_population",
            "value_type": "central",
            "value_status": value_status,
            "numeric_value": numeric_value,
            "unit": "count",
            "quality_grade": "L2",
        },
    )


async def test_loss_tables_and_constraints_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            assert {
                "loss_model_definitions",
                "loss_parameter_sets",
                "loss_products",
                "loss_metric_values",
                "loss_product_rasters",
            } <= tables
            constraints = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'loss_model_definitions'::regclass,
                            'loss_parameter_sets'::regclass,
                            'loss_products'::regclass,
                            'loss_metric_values'::regclass,
                            'loss_product_rasters'::regclass
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {
        row["conname"]: row["definition"]
        for row in constraints
    }
    assert "UNIQUE (model_id, formula_version)" in definitions[
        "uq_loss_model_formula"
    ]
    assert "UNIQUE (parameter_set_id, version)" in definitions[
        "uq_loss_parameter_version"
    ]
    assert "UNIQUE (run_id, product_type)" in definitions[
        "uq_loss_product_run_type"
    ]
    assert "UNIQUE (product_id, area_scope, area_code, metric_key, value_type)" in (
        definitions["uq_loss_metric_value"]
    )
    assert "UNIQUE (product_id, raster_version)" in definitions[
        "uq_loss_product_raster_version"
    ]


async def test_loss_product_uses_known_status_and_quality_values() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conname IN (
                            'ck_loss_product_status_value',
                            'ck_loss_product_quality_value'
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["conname"]: row["definition"] for row in rows}
    status_definition = definitions["ck_loss_product_status_value"]
    for value in ("complete", "partial", "unavailable", "invalid"):
        assert f"'{value}'" in status_definition
    quality_definition = definitions["ck_loss_product_quality_value"]
    for value in ("L1", "L2", "L3", "L0"):
        assert f"'{value}'" in quality_definition


async def test_loss_metric_value_accepts_valid_boundary_rows() -> None:
    cases = (
        ("available", 0),
        ("zero", 0),
        ("rounded_to_zero", 0.001),
        ("unavailable", None),
        ("not_applicable", None),
    )
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            product_id = await _insert_loss_product(connection)
            for index, (value_status, numeric_value) in enumerate(cases):
                await _insert_metric(
                    connection,
                    product_id=product_id,
                    value_status=value_status,
                    numeric_value=numeric_value,
                    area_code=f"valid-{index}",
                )
            count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM loss_metric_values
                    WHERE product_id = CAST(:product_id AS uuid)
                    """
                ),
                {"product_id": product_id},
            )
            assert count == len(cases)
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("value_status", ("unavailable", "not_applicable"))
async def test_loss_metric_value_requires_null_for_missing_statuses(
    value_status: str,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            product_id = await _insert_loss_product(connection)
            await _insert_metric(
                connection,
                product_id=product_id,
                value_status=value_status,
                numeric_value=None,
                area_code="valid",
            )
            nested = await connection.begin_nested()
            with pytest.raises(IntegrityError) as exc_info:
                await _insert_metric(
                    connection,
                    product_id=product_id,
                    value_status=value_status,
                    numeric_value=1,
                    area_code="invalid",
                )
            assert "ck_loss_metric_value" in str(exc_info.value)
            await nested.rollback()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("value_status", "numeric_value"),
    (
        ("available", None),
        ("available", -1),
        ("zero", None),
        ("zero", -1),
        ("rounded_to_zero", None),
        ("rounded_to_zero", -1),
    ),
)
async def test_loss_metric_value_rejects_null_or_negative_numeric_statuses(
    value_status: str,
    numeric_value: float | None,
) -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            product_id = await _insert_loss_product(connection)
            nested = await connection.begin_nested()
            with pytest.raises(IntegrityError) as exc_info:
                await _insert_metric(
                    connection,
                    product_id=product_id,
                    value_status=value_status,
                    numeric_value=numeric_value,
                    area_code="invalid",
                )
            assert "ck_loss_metric_value" in str(exc_info.value)
            await nested.rollback()
            await transaction.rollback()
    finally:
        await engine.dispose()


async def test_loss_metric_value_requires_zero_status_to_have_zero() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            product_id = await _insert_loss_product(connection)
            nested = await connection.begin_nested()
            with pytest.raises(IntegrityError) as exc_info:
                await _insert_metric(
                    connection,
                    product_id=product_id,
                    value_status="zero",
                    numeric_value=1,
                    area_code="invalid",
                )
            assert "ck_loss_metric_zero_value" in str(exc_info.value)
            await nested.rollback()
            await transaction.rollback()
    finally:
        await engine.dispose()
