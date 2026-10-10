"""Add operations automation persistence tables."""

import uuid
from collections.abc import Sequence
from datetime import time

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0026_operations_automation"
down_revision: str | None = "0025_qa_audit_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)
JSONB_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "operations_settings",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("settings_key", sa.String(length=32), nullable=False),
        sa.Column(
            "daily_test_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'Asia/Shanghai'"),
            nullable=False,
        ),
        sa.Column("daily_window_start", sa.Time(), nullable=False),
        sa.Column("daily_window_end", sa.Time(), nullable=False),
        sa.Column(
            "test_magnitude_min",
            sa.Numeric(precision=4, scale=1),
            server_default=sa.text("5.0"),
            nullable=False,
        ),
        sa.Column(
            "test_magnitude_max",
            sa.Numeric(precision=4, scale=1),
            server_default=sa.text("5.9"),
            nullable=False,
        ),
        sa.Column(
            "test_depth_min_km",
            sa.Numeric(precision=6, scale=2),
            server_default=sa.text("5.00"),
            nullable=False,
        ),
        sa.Column(
            "test_depth_max_km",
            sa.Numeric(precision=6, scale=2),
            server_default=sa.text("20.00"),
            nullable=False,
        ),
        sa.Column(
            "drill_conflict_policy",
            sa.String(length=16),
            server_default=sa.text("'pause'"),
            nullable=False,
        ),
        sa.Column(
            "row_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "test_magnitude_min BETWEEN -2.0 AND 12.0 "
            "AND test_magnitude_max BETWEEN -2.0 AND 12.0",
            name="ck_operations_settings_magnitude_range",
        ),
        sa.CheckConstraint(
            "test_depth_min_km BETWEEN 0 AND 1000 "
            "AND test_depth_max_km BETWEEN 0 AND 1000",
            name="ck_operations_settings_depth_range",
        ),
        sa.CheckConstraint(
            "test_magnitude_min <= test_magnitude_max",
            name="ck_operations_settings_magnitude_order",
        ),
        sa.CheckConstraint(
            "test_depth_min_km <= test_depth_max_km",
            name="ck_operations_settings_depth_order",
        ),
        sa.CheckConstraint(
            "daily_window_start < daily_window_end",
            name="ck_operations_settings_window_order",
        ),
        sa.CheckConstraint(
            "drill_conflict_policy IN ('pause', 'terminate')",
            name="ck_operations_settings_drill_conflict_policy",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "settings_key",
            name="uq_operations_settings_key",
        ),
    )

    op.create_table(
        "operations_test_schedules",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("scenario", JSONB_TYPE, nullable=True),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'due', 'deferred', 'triggered', 'skipped', 'failed')",
            name="ck_operations_test_schedule_state",
        ),
        sa.CheckConstraint(
            "state <> 'triggered' "
            "OR (event_id IS NOT NULL AND triggered_at IS NOT NULL)",
            name="ck_operations_test_schedule_triggered",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "local_date",
            name="uq_operations_test_schedule_local_date",
        ),
    )
    op.create_index(
        "ix_operations_test_schedules_event_id",
        "operations_test_schedules",
        ["event_id"],
        unique=False,
    )
    op.create_index(
        "ix_operations_test_schedules_state",
        "operations_test_schedules",
        ["state"],
        unique=False,
    )

    op.create_table(
        "operations_runs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("schedule_id", UUID_TYPE, nullable=True),
        sa.Column("trigger_mode", sa.String(length=32), nullable=False),
        sa.Column("operation_state", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column(
            "resume_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "detail",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trigger_mode IN ('scheduled_test', 'manual_test', 'drill', 'manual_event')",
            name="ck_operations_run_trigger_mode",
        ),
        sa.CheckConstraint(
            "operation_state IN ('queued', 'running', 'paused', "
            "'completed', 'canceled', 'failed')",
            name="ck_operations_run_operation_state",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["operations_test_schedules.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_operations_run_event"),
        sa.UniqueConstraint("schedule_id", name="uq_operations_run_schedule"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_operations_run_idempotency",
        ),
    )
    op.create_index(
        "ix_operations_runs_operation_state",
        "operations_runs",
        ["operation_state"],
        unique=False,
    )
    op.create_index(
        "ix_operations_runs_priority",
        "operations_runs",
        ["priority"],
        unique=False,
    )

    op.create_table(
        "operations_audit_logs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("before_state", sa.String(length=32), nullable=False),
        sa.Column("after_state", sa.String(length=32), nullable=False),
        sa.Column(
            "detail",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operations_audit_logs_resource",
        "operations_audit_logs",
        ["resource_type", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_operations_audit_logs_actor_created",
        "operations_audit_logs",
        ["actor", "created_at"],
        unique=False,
    )

    operations_settings = sa.table(
        "operations_settings",
        sa.column("id", UUID_TYPE),
        sa.column("settings_key", sa.String(length=32)),
        sa.column("daily_window_start", sa.Time()),
        sa.column("daily_window_end", sa.Time()),
    )
    op.bulk_insert(
        operations_settings,
        [
            {
                "id": uuid.uuid4(),
                "settings_key": "default",
                "daily_window_start": time(2, 0),
                "daily_window_end": time(4, 0),
            }
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operations_audit_logs_actor_created",
        table_name="operations_audit_logs",
    )
    op.drop_index(
        "ix_operations_audit_logs_resource",
        table_name="operations_audit_logs",
    )
    op.drop_table("operations_audit_logs")

    op.drop_index(
        "ix_operations_runs_priority",
        table_name="operations_runs",
    )
    op.drop_index(
        "ix_operations_runs_operation_state",
        table_name="operations_runs",
    )
    op.drop_table("operations_runs")

    op.drop_index(
        "ix_operations_test_schedules_state",
        table_name="operations_test_schedules",
    )
    op.drop_index(
        "ix_operations_test_schedules_event_id",
        table_name="operations_test_schedules",
    )
    op.drop_table("operations_test_schedules")

    op.drop_table("operations_settings")
