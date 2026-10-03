import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


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
