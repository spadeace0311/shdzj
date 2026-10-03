"""Backfill legacy users into workgroup memberships."""

from collections.abc import Sequence

from alembic import op


revision: str = "0023_workgroup_backfill"
down_revision: str | None = "0022_cleanup_namespace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_workgroup_membership_deputy_order_required",
        "workgroup_memberships",
        type_="check",
    )
    op.drop_constraint(
        "ck_workgroup_attendance_deputy_order_required",
        "workgroup_attendance",
        type_="check",
    )

    op.execute(
        """
        WITH workgroup_map(source_name, workgroup_code) AS (
            VALUES
                ('新闻信息值守组', 'news_information'),
                ('news_information', 'news_information'),
                ('监测预报组', 'monitoring_forecast'),
                ('monitoring_forecast', 'monitoring_forecast'),
                ('综合协调组', 'comprehensive_coordination'),
                ('comprehensive_coordination', 'comprehensive_coordination'),
                ('震害评估组', 'damage_assessment'),
                ('damage_assessment', 'damage_assessment'),
                ('应急技术组', 'emergency_technology'),
                ('emergency_technology', 'emergency_technology'),
                ('后勤保障组', 'logistics'),
                ('logistics', 'logistics'),
                ('中心站组', 'center_station'),
                ('center_station', 'center_station')
        ),
        mapped AS (
            SELECT
                u.id AS user_id,
                workgroup_map.workgroup_code,
                CASE u.role
                    WHEN 'group_leader' THEN 'leader'
                    WHEN 'group_deputy' THEN 'deputy'
                    WHEN 'group_member' THEN 'member'
                    WHEN 'viewer' THEN 'viewer'
                END AS duty_role,
                u.created_at
            FROM users AS u
            JOIN workgroup_map
              ON workgroup_map.source_name = u.workgroup
            WHERE u.is_active
              AND u.role IN (
                  'group_leader',
                  'group_deputy',
                  'group_member',
                  'viewer'
              )
        ),
        resolved AS (
            SELECT
                mapped.user_id,
                mapped.workgroup_code,
                CASE
                    WHEN mapped.duty_role = 'leader'
                     AND (
                         row_number() OVER (
                             PARTITION BY
                                 mapped.workgroup_code,
                                 mapped.duty_role
                             ORDER BY mapped.created_at, mapped.user_id
                         ) > 1
                         OR EXISTS (
                             SELECT 1
                             FROM workgroup_memberships AS existing
                             WHERE existing.workgroup_code
                                   = mapped.workgroup_code
                               AND existing.duty_role = 'leader'
                               AND existing.is_active
                               AND existing.effective_to IS NULL
                         )
                     )
                    THEN 'member'
                    ELSE mapped.duty_role
                END AS duty_role
            FROM mapped
        )
        INSERT INTO workgroup_memberships (
            id,
            user_id,
            workgroup_code,
            duty_role,
            deputy_order,
            effective_from,
            is_active,
            created_by,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            resolved.user_id,
            resolved.workgroup_code,
            resolved.duty_role,
            NULL,
            now(),
            true,
            'migration-0023',
            now(),
            now()
        FROM resolved
        WHERE NOT EXISTS (
            SELECT 1
            FROM workgroup_memberships AS existing
            WHERE existing.user_id = resolved.user_id
              AND existing.workgroup_code = resolved.workgroup_code
              AND existing.is_active
              AND existing.effective_to IS NULL
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE workgroup_memberships
        SET duty_role = 'member',
            updated_at = now()
        WHERE duty_role = 'deputy'
          AND deputy_order IS NULL
        """
    )
    op.execute(
        """
        UPDATE workgroup_attendance
        SET duty_role_in_snapshot = 'member',
            updated_at = now()
        WHERE duty_role_in_snapshot = 'deputy'
          AND deputy_order_in_snapshot IS NULL
        """
    )
    op.execute(
        """
        DELETE FROM workgroup_memberships
        WHERE created_by = 'migration-0023'
        """
    )
    op.create_check_constraint(
        "ck_workgroup_membership_deputy_order_required",
        "workgroup_memberships",
        "duty_role != 'deputy' OR deputy_order IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_workgroup_attendance_deputy_order_required",
        "workgroup_attendance",
        "duty_role_in_snapshot != 'deputy' "
        "OR deputy_order_in_snapshot IS NOT NULL",
    )
