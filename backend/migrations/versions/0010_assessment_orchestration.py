"""Add assessment orchestration runs and tasks."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010_assessment_orchestration"
down_revision: str | None = "0009_non_cenc_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assessment_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_no", sa.Integer(), nullable=False),
        sa.Column("trigger_reason", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column("t1_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["event_lifecycle_outbox.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", name="uq_assessment_runs_revision"),
        sa.UniqueConstraint("outbox_id", name="uq_assessment_runs_outbox"),
        sa.UniqueConstraint(
            "event_id",
            "run_no",
            name="uq_assessment_runs_event_run_no",
        ),
    )
    op.create_index("ix_assessment_runs_event_id", "assessment_runs", ["event_id"])
    op.create_index("ix_assessment_runs_revision_id", "assessment_runs", ["revision_id"])
    op.create_index("ix_assessment_runs_outbox_id", "assessment_runs", ["outbox_id"])
    op.create_index("ix_assessment_runs_status", "assessment_runs", ["status"])
    op.create_index("ix_assessment_runs_t1_at", "assessment_runs", ["t1_at"])
    op.create_index("ix_assessment_runs_deadline_at", "assessment_runs", ["deadline_at"])
    op.create_index(
        "ix_assessment_runs_status_deadline",
        "assessment_runs",
        ["status", "deadline_at"],
    )

    op.create_table(
        "assessment_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_key", sa.String(length=96), nullable=False),
        sa.Column("task_type", sa.String(length=48), nullable=False),
        sa.Column("component", sa.String(length=48), nullable=False),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assessment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "task_key", name="uq_assessment_tasks_run_task_key"),
    )
    op.create_index("ix_assessment_tasks_run_id", "assessment_tasks", ["run_id"])
    op.create_index("ix_assessment_tasks_task_type", "assessment_tasks", ["task_type"])
    op.create_index("ix_assessment_tasks_component", "assessment_tasks", ["component"])
    op.create_index("ix_assessment_tasks_status", "assessment_tasks", ["status"])
    op.create_index("ix_assessment_tasks_deadline_at", "assessment_tasks", ["deadline_at"])
    op.create_index(
        "ix_assessment_tasks_status_deadline",
        "assessment_tasks",
        ["status", "deadline_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_tasks_status_deadline",
        table_name="assessment_tasks",
    )
    op.drop_index("ix_assessment_tasks_deadline_at", table_name="assessment_tasks")
    op.drop_index("ix_assessment_tasks_status", table_name="assessment_tasks")
    op.drop_index("ix_assessment_tasks_component", table_name="assessment_tasks")
    op.drop_index("ix_assessment_tasks_task_type", table_name="assessment_tasks")
    op.drop_index("ix_assessment_tasks_run_id", table_name="assessment_tasks")
    op.drop_table("assessment_tasks")

    op.drop_index("ix_assessment_runs_status_deadline", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_deadline_at", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_t1_at", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_status", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_outbox_id", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_revision_id", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_event_id", table_name="assessment_runs")
    op.drop_table("assessment_runs")
