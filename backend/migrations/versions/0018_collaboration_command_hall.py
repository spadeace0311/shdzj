"""Add workgroup collaboration and command hall persistence."""

import uuid
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0018_collaboration_command_hall"
down_revision: str | None = "0017_production_cancel_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)
JSONB_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "workgroup_definitions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_workgroup_definition_code"),
        sa.UniqueConstraint(
            "display_order",
            name="uq_workgroup_definition_display_order",
        ),
    )

    op.create_table(
        "workgroup_memberships",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", UUID_TYPE, nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column("duty_role", sa.String(length=16), nullable=False),
        sa.Column("deputy_order", sa.Integer(), nullable=True),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "effective_to",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
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
            "duty_role != 'deputy' OR deputy_order IS NOT NULL",
            name="ck_workgroup_membership_deputy_order_required",
        ),
        sa.CheckConstraint(
            "duty_role IN ('leader', 'deputy', 'member', 'viewer')",
            name="ck_workgroup_membership_duty_role",
        ),
        sa.CheckConstraint(
            "deputy_order IS NULL OR deputy_order > 0",
            name="ck_workgroup_membership_deputy_order_positive",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workgroup_memberships_user_id",
        "workgroup_memberships",
        ["user_id"],
    )
    op.create_index(
        "ix_workgroup_memberships_workgroup_code",
        "workgroup_memberships",
        ["workgroup_code"],
    )
    op.create_index(
        "uq_workgroup_membership_active_deputy_order",
        "workgroup_memberships",
        ["workgroup_code", "deputy_order"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND duty_role = 'deputy' AND effective_to IS NULL"
        ),
    )
    op.create_index(
        "uq_workgroup_membership_active_leader",
        "workgroup_memberships",
        ["workgroup_code"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND duty_role = 'leader' AND effective_to IS NULL"
        ),
    )

    op.create_table(
        "workgroup_roster_snapshots",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column("roster_version", sa.Integer(), nullable=False),
        sa.Column("roster_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("leader_user_id", UUID_TYPE, nullable=True),
        sa.Column(
            "deputies",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "members",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("snapshot", JSONB_TYPE, nullable=False),
        sa.Column(
            "created_at",
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
            ["leader_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "workgroup_code",
            "roster_version",
            name="uq_workgroup_roster_event_group_version",
        ),
    )
    op.create_index(
        "ix_workgroup_roster_snapshots_event_id",
        "workgroup_roster_snapshots",
        ["event_id"],
    )
    op.create_index(
        "ix_workgroup_roster_snapshots_workgroup_code",
        "workgroup_roster_snapshots",
        ["workgroup_code"],
    )

    op.create_table(
        "workgroup_attendance",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column("user_id", UUID_TYPE, nullable=False),
        sa.Column("duty_role_in_snapshot", sa.String(length=16), nullable=False),
        sa.Column("deputy_order_in_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column(
            "checked_in_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "checked_out_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
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
            "state IN ('unknown', 'present', 'absent', 'departed')",
            name="ck_workgroup_attendance_state",
        ),
        sa.CheckConstraint(
            "duty_role_in_snapshot IN ('leader', 'deputy', 'member', 'viewer')",
            name="ck_workgroup_attendance_snapshot_role",
        ),
        sa.CheckConstraint(
            "duty_role_in_snapshot != 'deputy' "
            "OR deputy_order_in_snapshot IS NOT NULL",
            name="ck_workgroup_attendance_deputy_order_required",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "workgroup_code",
            "user_id",
            name="uq_workgroup_attendance_event_group_user",
        ),
    )
    op.create_index(
        "ix_workgroup_attendance_event_id",
        "workgroup_attendance",
        ["event_id"],
    )
    op.create_index(
        "ix_workgroup_attendance_user_id",
        "workgroup_attendance",
        ["user_id"],
    )
    op.create_index(
        "ix_workgroup_attendance_workgroup_code",
        "workgroup_attendance",
        ["workgroup_code"],
    )

    op.create_table(
        "collaboration_settings",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column(
            "intensity_threshold",
            sa.Numeric(precision=4, scale=1),
            server_default=sa.text("2.0"),
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
            "intensity_threshold >= 0",
            name="ck_collaboration_settings_intensity_threshold",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "collaboration_task_templates",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
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
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_collaboration_task_template_code"),
    )
    op.create_index(
        "ix_collaboration_task_templates_workgroup_code",
        "collaboration_task_templates",
        ["workgroup_code"],
    )

    op.create_table(
        "collaboration_task_template_versions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("template_id", UUID_TYPE, nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("template_code", sa.String(length=160), nullable=False),
        sa.Column("phase_code", sa.String(length=64), nullable=False),
        sa.Column(
            "start_offset_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("due_offset_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "continues_until_response_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "required_deliverables",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "optional_deliverables",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "artifact_bindings",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "applicability",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "instruction",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("response_basis", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "start_offset_seconds >= 0",
            name="ck_collaboration_template_start_offset",
        ),
        sa.CheckConstraint(
            "due_offset_seconds IS NULL "
            "OR due_offset_seconds >= start_offset_seconds",
            name="ck_collaboration_template_due_offset",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["collaboration_task_templates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_code",
            "version",
            name="uq_collaboration_task_template_code_version",
        ),
        sa.UniqueConstraint(
            "template_id",
            "version",
            name="uq_collaboration_task_template_version",
        ),
    )
    op.create_index(
        "ix_collaboration_task_template_versions_template_id",
        "collaboration_task_template_versions",
        ["template_id"],
    )
    op.create_index(
        "ix_collaboration_task_template_versions_template_code",
        "collaboration_task_template_versions",
        ["template_code"],
    )
    op.create_index(
        "ix_collaboration_task_template_versions_published_at",
        "collaboration_task_template_versions",
        ["published_at"],
    )

    op.create_table(
        "collaboration_tasks",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("trigger_revision_id", UUID_TYPE, nullable=False),
        sa.Column("assessment_run_id", UUID_TYPE, nullable=True),
        sa.Column("template_version_id", UUID_TYPE, nullable=True),
        sa.Column("task_code", sa.String(length=160), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=256), nullable=True),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column(
            "instruction",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default=sa.text("100"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "timeliness_state",
            sa.String(length=32),
            server_default=sa.text("'on_time'"),
            nullable=False,
        ),
        sa.Column("phase_code", sa.String(length=64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "row_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
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
            "source_type IN ('preplan', 'correction', 'ad_hoc', 'system_review')",
            name="ck_collaboration_task_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'pending_review', "
            "'completed', 'not_required', 'failed')",
            name="ck_collaboration_task_status",
        ),
        sa.CheckConstraint(
            "timeliness_state IN ('on_time', 'at_risk', 'overdue')",
            name="ck_collaboration_task_timeliness",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"],
            ["assessment_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["collaboration_task_template_versions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_revision_id"],
            ["earthquake_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "task_code",
            name="uq_collaboration_task_event_code",
        ),
    )
    op.create_index(
        "ix_collaboration_tasks_event_id",
        "collaboration_tasks",
        ["event_id"],
    )
    op.create_index(
        "ix_collaboration_tasks_trigger_revision_id",
        "collaboration_tasks",
        ["trigger_revision_id"],
    )
    op.create_index(
        "ix_collaboration_tasks_assessment_run_id",
        "collaboration_tasks",
        ["assessment_run_id"],
    )
    op.create_index(
        "ix_collaboration_tasks_template_version_id",
        "collaboration_tasks",
        ["template_version_id"],
    )
    op.create_index(
        "ix_collaboration_tasks_workgroup_code",
        "collaboration_tasks",
        ["workgroup_code"],
    )
    op.create_index(
        "ix_collaboration_tasks_due_at",
        "collaboration_tasks",
        ["due_at"],
    )
    op.create_index(
        "ix_collaboration_tasks_status_due",
        "collaboration_tasks",
        ["status", "due_at"],
    )
    op.create_index(
        "ix_collaboration_tasks_group_status",
        "collaboration_tasks",
        ["workgroup_code", "status"],
    )

    op.create_table(
        "collaboration_task_contributors",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("task_id", UUID_TYPE, nullable=False),
        sa.Column("user_id", UUID_TYPE, nullable=False),
        sa.Column(
            "contribution_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "first_contributed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_contributed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collaboration_task_contributors_task_id",
        "collaboration_task_contributors",
        ["task_id"],
    )
    op.create_index(
        "ix_collaboration_task_contributors_user_id",
        "collaboration_task_contributors",
        ["user_id"],
    )
    op.create_index(
        "uq_collaboration_task_contributor",
        "collaboration_task_contributors",
        ["task_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "collaboration_task_deliverables",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("task_id", UUID_TYPE, nullable=False),
        sa.Column("deliverable_code", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column(
            "is_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("requirement_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_binding", JSONB_TYPE, nullable=True),
        sa.Column(
            "display_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
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
            "requirement_kind IN ('automatic_artifact', 'manual_file', "
            "'manual_text', 'manual_file_or_text')",
            name="ck_collaboration_deliverable_requirement_kind",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "deliverable_code",
            name="uq_collaboration_task_deliverable_code",
        ),
    )
    op.create_index(
        "ix_collaboration_task_deliverables_task_id",
        "collaboration_task_deliverables",
        ["task_id"],
    )

    op.create_table(
        "collaboration_deliverable_versions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("deliverable_id", UUID_TYPE, nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", UUID_TYPE, nullable=True),
        sa.Column("artifact_publication_id", UUID_TYPE, nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("file_name", sa.String(length=512), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("text_result", JSONB_TYPE, nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("basis_text", sa.Text(), nullable=True),
        sa.Column("supersedes_version_id", UUID_TYPE, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('automatic', 'manual', 'superadmin_override')",
            name="ck_collaboration_deliverable_source_kind",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["generated_artifacts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_publication_id"],
            ["artifact_publications.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deliverable_id"],
            ["collaboration_task_deliverables.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["collaboration_deliverable_versions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deliverable_id",
            "version_no",
            name="uq_collaboration_deliverable_version_no",
        ),
    )
    op.create_index(
        "ix_collaboration_deliverable_versions_deliverable_id",
        "collaboration_deliverable_versions",
        ["deliverable_id"],
    )
    op.create_index(
        "ix_collaboration_deliverable_versions_artifact_id",
        "collaboration_deliverable_versions",
        ["artifact_id"],
    )
    op.create_index(
        "ix_collaboration_deliverable_versions_artifact_publication_id",
        "collaboration_deliverable_versions",
        ["artifact_publication_id"],
    )
    op.create_index(
        "uq_collaboration_deliverable_artifact_publication",
        "collaboration_deliverable_versions",
        ["deliverable_id", "artifact_publication_id"],
        unique=True,
        postgresql_where=sa.text("artifact_publication_id IS NOT NULL"),
    )

    op.create_table(
        "collaboration_deliverable_publications",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("deliverable_id", UUID_TYPE, nullable=False),
        sa.Column("version_id", UUID_TYPE, nullable=False),
        sa.Column("published_by", sa.String(length=64), nullable=False),
        sa.Column("published_role", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("publication_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "published_role IN ('leader', 'deputy', 'superadmin')",
            name="ck_collaboration_publication_role",
        ),
        sa.ForeignKeyConstraint(
            ["deliverable_id"],
            ["collaboration_task_deliverables.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["collaboration_deliverable_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collaboration_deliverable_publications_deliverable_id",
        "collaboration_deliverable_publications",
        ["deliverable_id"],
    )
    op.create_index(
        "ix_collaboration_deliverable_publications_version_id",
        "collaboration_deliverable_publications",
        ["version_id"],
    )
    op.create_index(
        "uq_collaboration_deliverable_current_publication",
        "collaboration_deliverable_publications",
        ["deliverable_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "collaboration_task_events",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("task_id", UUID_TYPE, nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("business_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "payload",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "event_seq",
            name="uq_collaboration_task_event_sequence",
        ),
    )
    op.create_index(
        "ix_collaboration_task_events_task_id",
        "collaboration_task_events",
        ["task_id"],
    )
    op.create_index(
        "ix_collaboration_task_events_event_type",
        "collaboration_task_events",
        ["event_type"],
    )
    op.create_index(
        "uq_collaboration_task_event_idempotency",
        "collaboration_task_events",
        ["task_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "collaboration_notification_deliveries",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("task_id", UUID_TYPE, nullable=True),
        sa.Column("recipient_user_id", UUID_TYPE, nullable=False),
        sa.Column("intent_type", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=256), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
            "intent_type IN ('assigned', 'due_soon', 'overdue', "
            "'status_changed', 'failure')",
            name="ck_collaboration_notification_intent",
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'wecom', 'email', 'phone')",
            name="ck_collaboration_notification_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', "
            "'fallback_sent')",
            name="ck_collaboration_notification_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "task_id",
            "recipient_user_id",
            "channel",
            "dedupe_key",
            name="uq_collaboration_notification_delivery",
        ),
    )
    op.create_index(
        "ix_collaboration_notification_deliveries_event_id",
        "collaboration_notification_deliveries",
        ["event_id"],
    )
    op.create_index(
        "ix_collaboration_notification_deliveries_task_id",
        "collaboration_notification_deliveries",
        ["task_id"],
    )
    op.create_index(
        "ix_collaboration_notification_deliveries_recipient_user_id",
        "collaboration_notification_deliveries",
        ["recipient_user_id"],
    )
    op.create_index(
        "ix_collaboration_notifications_pending",
        "collaboration_notification_deliveries",
        ["status", "available_at"],
    )

    op.create_table(
        "collaboration_projection_outbox",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("task_id", UUID_TYPE, nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column(
            "payload",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_collaboration_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collaboration_projection_outbox_event_id",
        "collaboration_projection_outbox",
        ["event_id"],
    )
    op.create_index(
        "ix_collaboration_projection_outbox_task_id",
        "collaboration_projection_outbox",
        ["task_id"],
    )
    op.create_index(
        "ix_collaboration_projection_outbox_event_type",
        "collaboration_projection_outbox",
        ["event_type"],
    )
    op.create_index(
        "ix_collaboration_outbox_pending",
        "collaboration_projection_outbox",
        ["status", "available_at"],
    )
    op.create_index(
        "uq_collaboration_outbox_idempotency",
        "collaboration_projection_outbox",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "command_hall_event_projections",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column(
            "event_snapshot",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "task_counts",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "artifact_summary",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "alert_summary",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "projection_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_command_hall_event_projection_event",
        ),
    )
    op.create_index(
        "ix_command_hall_event_projections_event_id",
        "command_hall_event_projections",
        ["event_id"],
    )

    op.create_table(
        "command_hall_group_projections",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=False),
        sa.Column(
            "group_snapshot",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "task_counts",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("latest_deliverable", JSONB_TYPE, nullable=True),
        sa.Column(
            "alert_summary",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "projection_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "is_current",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
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
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_command_hall_group_projections_event_id",
        "command_hall_group_projections",
        ["event_id"],
    )
    op.create_index(
        "ix_command_hall_group_projections_workgroup_code",
        "command_hall_group_projections",
        ["workgroup_code"],
    )
    op.create_index(
        "uq_command_hall_group_projection_event_group",
        "command_hall_group_projections",
        ["event_id", "workgroup_code"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "command_hall_alert_projections",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("workgroup_code", sa.String(length=64), nullable=True),
        sa.Column("task_id", UUID_TYPE, nullable=True),
        sa.Column("alert_key", sa.String(length=160), nullable=False),
        sa.Column("alert_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column(
            "detail",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "projection_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_command_hall_alert_severity",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["collaboration_tasks.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_code"],
            ["workgroup_definitions.code"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_command_hall_alert_projections_event_id",
        "command_hall_alert_projections",
        ["event_id"],
    )
    op.create_index(
        "ix_command_hall_alert_projections_workgroup_code",
        "command_hall_alert_projections",
        ["workgroup_code"],
    )
    op.create_index(
        "ix_command_hall_alert_projections_task_id",
        "command_hall_alert_projections",
        ["task_id"],
    )
    op.create_index(
        "ix_command_hall_alerts_event_status",
        "command_hall_alert_projections",
        ["event_id", "status"],
    )

    workgroup_table = sa.table(
        "workgroup_definitions",
        sa.column("id", UUID_TYPE),
        sa.column("code", sa.String(length=64)),
        sa.column("name", sa.String(length=128)),
        sa.column("display_order", sa.Integer()),
    )
    op.bulk_insert(
        workgroup_table,
        [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "code": "news_information",
                "name": "新闻信息值守组",
                "display_order": 1,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
                "code": "monitoring_forecast",
                "name": "监测预报组",
                "display_order": 2,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000003"),
                "code": "comprehensive_coordination",
                "name": "综合协调组",
                "display_order": 3,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000004"),
                "code": "damage_assessment",
                "name": "震害评估组",
                "display_order": 4,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000005"),
                "code": "emergency_technology",
                "name": "应急技术组",
                "display_order": 5,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000006"),
                "code": "logistics",
                "name": "后勤保障组",
                "display_order": 6,
            },
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000007"),
                "code": "center_station",
                "name": "中心站组",
                "display_order": 7,
            },
        ],
    )
    settings_table = sa.table(
        "collaboration_settings",
        sa.column("id", UUID_TYPE),
        sa.column("intensity_threshold", sa.Numeric(precision=4, scale=1)),
        sa.column("row_version", sa.Integer()),
    )
    op.bulk_insert(
        settings_table,
        [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000018"),
                "intensity_threshold": 2.0,
                "row_version": 1,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("command_hall_alert_projections")
    op.drop_table("command_hall_group_projections")
    op.drop_table("command_hall_event_projections")
    op.drop_table("collaboration_projection_outbox")
    op.drop_table("collaboration_notification_deliveries")
    op.drop_table("collaboration_task_events")
    op.drop_table("collaboration_deliverable_publications")
    op.drop_table("collaboration_deliverable_versions")
    op.drop_table("collaboration_task_deliverables")
    op.drop_table("collaboration_task_contributors")
    op.drop_table("collaboration_tasks")
    op.drop_table("collaboration_task_template_versions")
    op.drop_table("collaboration_task_templates")
    op.drop_table("collaboration_settings")
    op.drop_table("workgroup_attendance")
    op.drop_table("workgroup_roster_snapshots")
    op.drop_table("workgroup_memberships")
    op.drop_table("workgroup_definitions")
