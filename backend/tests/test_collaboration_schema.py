import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            async with session.begin():
                yield session
    finally:
        await engine.dispose()


async def _seed_task_identity_fixture(session):
    user_id = uuid.uuid4()
    event_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    template_id = uuid.uuid4()
    first_version_id = uuid.uuid4()
    second_version_id = uuid.uuid4()
    fixture_key = uuid.uuid4().hex

    await session.execute(
        text(
            """
            INSERT INTO users (
                id, username, password_hash, role, workgroup, is_active
            )
            VALUES (
                :user_id, :username, 'not-used', 'group_member',
                'monitoring_forecast', true
            )
            """
        ),
        {"user_id": user_id, "username": f"collaboration-{fixture_key}"},
    )
    await session.execute(
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
                :event_id,
                'collaboration-schema',
                :canonical_source_id,
                'test',
                now(),
                121.5,
                31.2,
                10,
                4.5,
                'collaboration schema fixture',
                ST_SetSRID(ST_MakePoint(121.5, 31.2), 4326)
            )
            """
        ),
        {
            "event_id": event_id,
            "canonical_source_id": f"collaboration-{fixture_key}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO raw_messages (
                id, source, message_kind, checksum, payload
            )
            VALUES (
                :raw_id,
                'collaboration-schema',
                'test',
                :checksum,
                '{}'::jsonb
            )
            """
        ),
        {
            "raw_id": raw_id,
            "checksum": f"{fixture_key}{uuid.uuid4().hex}",
        },
    )
    await session.execute(
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
                :revision_id,
                :event_id,
                :raw_id,
                1,
                'test',
                now(),
                121.5,
                31.2,
                10,
                4.5,
                'collaboration schema fixture'
            )
            """
        ),
        {
            "revision_id": revision_id,
            "event_id": event_id,
            "raw_id": raw_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO collaboration_task_templates (
                id, code, title, category, workgroup_code
            )
            VALUES (
                :template_id,
                :template_code,
                'identity fixture',
                'test',
                'monitoring_forecast'
            )
            """
        ),
        {
            "template_id": template_id,
            "template_code": f"identity-{fixture_key}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO collaboration_task_template_versions (
                id, template_id, version, template_code, phase_code
            )
            VALUES
                (
                    :first_version_id,
                    :template_id,
                    'v1',
                    :template_code,
                    'test'
                ),
                (
                    :second_version_id,
                    :template_id,
                    'v2',
                    :template_code,
                    'test'
                )
            """
        ),
        {
            "first_version_id": first_version_id,
            "second_version_id": second_version_id,
            "template_id": template_id,
            "template_code": f"identity-{fixture_key}",
        },
    )
    return {
        "user_id": user_id,
        "event_id": event_id,
        "raw_id": raw_id,
        "revision_id": revision_id,
        "template_id": template_id,
        "first_version_id": first_version_id,
        "second_version_id": second_version_id,
    }


async def _delete_task_identity_fixture(session, fixture) -> None:
    await session.execute(
        text("DELETE FROM collaboration_tasks WHERE event_id = :event_id"),
        {"event_id": fixture["event_id"]},
    )
    await session.execute(
        text(
            "DELETE FROM collaboration_task_templates WHERE id = :template_id"
        ),
        {"template_id": fixture["template_id"]},
    )
    await session.execute(
        text("DELETE FROM earthquake_events WHERE id = :event_id"),
        {"event_id": fixture["event_id"]},
    )
    await session.execute(
        text("DELETE FROM raw_messages WHERE id = :raw_id"),
        {"raw_id": fixture["raw_id"]},
    )
    await session.execute(
        text("DELETE FROM users WHERE id = :user_id"),
        {"user_id": fixture["user_id"]},
    )


async def _insert_task(
    session,
    *,
    event_id,
    revision_id,
    template_version_id,
    task_code: str,
    source_type: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO collaboration_tasks (
                id,
                event_id,
                trigger_revision_id,
                template_version_id,
                task_code,
                source_type,
                workgroup_code,
                title
            )
            VALUES (
                :id,
                :event_id,
                :revision_id,
                :template_version_id,
                :task_code,
                :source_type,
                'monitoring_forecast',
                :title
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "event_id": event_id,
            "revision_id": revision_id,
            "template_version_id": template_version_id,
            "task_code": task_code,
            "source_type": source_type,
            "title": task_code,
        },
    )


async def test_workgroups_are_fixed_to_seven(session):
    rows = (
        await session.execute(
            text(
                """
                SELECT code, display_order
                FROM workgroup_definitions
                WHERE is_active
                ORDER BY display_order
                """
            )
        )
    ).all()
    assert [(row.code, row.display_order) for row in rows] == [
        ("news_information", 1),
        ("monitoring_forecast", 2),
        ("comprehensive_coordination", 3),
        ("damage_assessment", 4),
        ("emergency_technology", 5),
        ("logistics", 6),
        ("center_station", 7),
    ]

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    UPDATE workgroup_definitions
                    SET code = 'unexpected_group'
                    WHERE code = 'news_information'
                    """
                )
            )
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    UPDATE workgroup_definitions
                    SET display_order = 8
                    WHERE code = 'center_station'
                    """
                )
            )
            await session.flush()


async def test_task_identity_is_template_version_aware(session):
    fixture = await _seed_task_identity_fixture(session)
    try:
        task_code = "identity.preplan"
        async with session.begin_nested():
            await _insert_task(
                session,
                event_id=fixture["event_id"],
                revision_id=fixture["revision_id"],
                template_version_id=fixture["first_version_id"],
                task_code=task_code,
                source_type="preplan",
            )
        async with session.begin_nested():
            await _insert_task(
                session,
                event_id=fixture["event_id"],
                revision_id=fixture["revision_id"],
                template_version_id=fixture["second_version_id"],
                task_code=task_code,
                source_type="preplan",
            )

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await _insert_task(
                    session,
                    event_id=fixture["event_id"],
                    revision_id=fixture["revision_id"],
                    template_version_id=fixture["first_version_id"],
                    task_code=task_code,
                    source_type="preplan",
                )
                await session.flush()

        ad_hoc_code = "identity.ad_hoc"
        async with session.begin_nested():
            await _insert_task(
                session,
                event_id=fixture["event_id"],
                revision_id=fixture["revision_id"],
                template_version_id=None,
                task_code=ad_hoc_code,
                source_type="ad_hoc",
            )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await _insert_task(
                    session,
                    event_id=fixture["event_id"],
                    revision_id=fixture["revision_id"],
                    template_version_id=None,
                    task_code=ad_hoc_code,
                    source_type="ad_hoc",
                )
                await session.flush()
    finally:
        await _delete_task_identity_fixture(session, fixture)


async def test_event_level_notification_deduplication(session):
    fixture = await _seed_task_identity_fixture(session)
    try:
        delivery = {
            "event_id": fixture["event_id"],
            "recipient_user_id": fixture["user_id"],
            "intent_type": "failure",
            "channel": "in_app",
            "dedupe_key": "event-failure",
        }
        await session.execute(
            text(
                """
                INSERT INTO collaboration_notification_deliveries (
                    id,
                    event_id,
                    recipient_user_id,
                    intent_type,
                    channel,
                    dedupe_key
                )
                VALUES (
                    :id,
                    :event_id,
                    :recipient_user_id,
                    :intent_type,
                    :channel,
                    :dedupe_key
                )
                """
            ),
            {"id": uuid.uuid4(), **delivery},
        )

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        INSERT INTO collaboration_notification_deliveries (
                            id,
                            event_id,
                            recipient_user_id,
                            intent_type,
                            channel,
                            dedupe_key
                        )
                        VALUES (
                            :id,
                            :event_id,
                            :recipient_user_id,
                            :intent_type,
                            :channel,
                            :dedupe_key
                        )
                        """
                    ),
                    {"id": uuid.uuid4(), **delivery},
                )
                await session.flush()
    finally:
        await _delete_task_identity_fixture(session, fixture)


async def test_collaboration_tables_and_deputy_order_constraint(session):
    connection = await session.connection()
    table_names = await connection.run_sync(
        lambda sync: set(inspect(sync).get_table_names())
    )
    assert {
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
    } <= table_names

    first_user = uuid.uuid4()
    second_user = uuid.uuid4()
    await session.execute(
        text("DELETE FROM users WHERE username IN ('deputy-1', 'deputy-2')")
    )
    await session.execute(
        text(
            """
            INSERT INTO users (
                id, username, password_hash, role, workgroup, is_active
            )
            VALUES
                (:first_user, 'deputy-1', 'not-used', 'group_deputy',
                 'monitoring_forecast', true),
                (:second_user, 'deputy-2', 'not-used', 'group_deputy',
                 'monitoring_forecast', true)
            """
        ),
        {"first_user": first_user, "second_user": second_user},
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO workgroup_memberships (
                        user_id, workgroup_code, duty_role, deputy_order
                    )
                    VALUES
                        (:first_user, 'monitoring_forecast', 'deputy', 1),
                        (:second_user, 'monitoring_forecast', 'deputy', 1)
                    """
                ),
                {"first_user": first_user, "second_user": second_user},
            )
            await session.flush()
