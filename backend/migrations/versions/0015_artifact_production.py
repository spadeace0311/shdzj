"""Add artifact production schema."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_artifact_production"
down_revision: str | None = "0014_loss_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "assessment_runs",
        "t1_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    dependency_kind_enum = postgresql.ENUM(
        "assessment_product",
        "artifact",
        name="artifact_dependency_kind",
    )
    dependency_kind_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "artifact_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_key", name="uq_artifact_template_key"),
    )

    op.create_table(
        "artifact_template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["artifact_templates.id"],
            name="fk_artifact_template_versions_template",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id",
            "version",
            name="uq_artifact_template_version",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_artifact_template_version_status",
        ),
    )
    op.create_index(
        "ix_artifact_template_versions_template_id",
        "artifact_template_versions",
        ["template_id"],
    )
    op.create_index(
        "ix_artifact_template_versions_status",
        "artifact_template_versions",
        ["status"],
    )

    op.create_table(
        "artifact_production_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("production_mode", sa.String(32), nullable=False),
        sa.Column("launch_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("deadline_basis_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_kind", sa.String(32), nullable=False),
        sa.Column("deadline_exceeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_artifact_committed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catalog_version", sa.String(128), nullable=False),
        sa.Column("input_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("context_fingerprint", sa.String(64), nullable=True),
        sa.Column("final_input_fingerprint", sa.String(64), nullable=True),
        sa.Column("generation_seq", sa.Integer(), nullable=False),
        sa.Column("generation_scope", sa.String(256), nullable=False),
        sa.Column("required_outputs", postgresql.JSONB(), nullable=False),
        sa.Column("rebuild_parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("superseded_by_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
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
            ["assessment_run_id"],
            ["assessment_runs.id"],
            name="fk_artifact_production_runs_assessment",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            name="fk_artifact_production_runs_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            name="fk_artifact_production_runs_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_run_id",
            "generation_seq",
            name="uq_artifact_run_generation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', "
            "'failed', 'canceled')",
            name="ck_artifact_run_status",
        ),
        sa.CheckConstraint(
            "production_mode IN ('live', 'manual', 'test', 'drill', 'replay')",
            name="ck_artifact_production_mode",
        ),
        sa.CheckConstraint(
            "launch_mode IN ('assessment_child', 'standalone')",
            name="ck_artifact_launch_mode",
        ),
        sa.CheckConstraint(
            "deadline_kind IN ('event_deadline', 'rebuild_deadline')",
            name="ck_artifact_deadline_kind",
        ),
    )
    op.create_index(
        "ix_artifact_production_runs_assessment_run_id",
        "artifact_production_runs",
        ["assessment_run_id"],
    )
    op.create_index(
        "ix_artifact_production_runs_event_id",
        "artifact_production_runs",
        ["event_id"],
    )
    op.create_index(
        "ix_artifact_production_runs_revision_id",
        "artifact_production_runs",
        ["revision_id"],
    )
    op.create_index(
        "ix_artifact_production_runs_status",
        "artifact_production_runs",
        ["status"],
    )
    op.create_index(
        "ix_artifact_production_runs_is_current",
        "artifact_production_runs",
        ["is_current"],
    )
    op.create_index(
        "uq_artifact_run_current_scope",
        "artifact_production_runs",
        ["event_id", "revision_id", "generation_scope"],
        unique=True,
        postgresql_where=sa.text("is_current AND superseded_at IS NULL"),
    )

    op.create_table(
        "production_input_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("region_id", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_production_input_snapshots_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_run_id",
            name="uq_production_input_snapshot_run",
        ),
    )
    op.create_index(
        "ix_production_input_snapshots_production_run_id",
        "production_input_snapshots",
        ["production_run_id"],
    )

    op.create_table(
        "production_input_snapshot_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_key", sa.String(160), nullable=False),
        sa.Column("asset_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column(
            "selected_for_render",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["production_input_snapshots.id"],
            name="fk_production_input_snapshot_items_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"],
            ["data_asset_versions.id"],
            name="fk_production_input_snapshot_items_asset_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "asset_key",
            "role",
            name="uq_production_input_snapshot_item",
        ),
    )
    op.create_index(
        "ix_production_input_snapshot_items_snapshot_id",
        "production_input_snapshot_items",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_production_input_snapshot_items_asset_version_id",
        "production_input_snapshot_items",
        ["asset_version_id"],
    )

    op.create_table(
        "artifact_production_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_key", sa.String(160), nullable=False),
        sa.Column("output_profile", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "depends_on",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "optional_depends_on",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "optional_dependency_wait_cutoff_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("final_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deadline_exceeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
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
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_artifact_production_tasks_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_run_id",
            "artifact_key",
            "output_profile",
            name="uq_artifact_task_output",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'succeeded', "
            "'degraded', 'failed', 'timed_out', 'canceled')",
            name="ck_artifact_task_status",
        ),
    )
    op.create_index(
        "ix_artifact_production_tasks_run_id",
        "artifact_production_tasks",
        ["production_run_id"],
    )
    op.create_index(
        "ix_artifact_production_tasks_status",
        "artifact_production_tasks",
        ["status"],
    )

    op.create_table(
        "artifact_task_dependency_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "dependency_kind",
            postgresql.ENUM(
                "assessment_product",
                "artifact",
                name="artifact_dependency_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("dependency_key", sa.String(160), nullable=False),
        sa.Column("dependency_output_profile", sa.String(64), nullable=True),
        sa.Column("is_optional", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("bound_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bound_version", sa.String(128), nullable=True),
        sa.Column("bound_checksum", sa.String(64), nullable=True),
        sa.Column("resolution_status", sa.String(32), nullable=False),
        sa.Column("resolution_detail", postgresql.JSONB(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_task_id"],
            ["artifact_production_tasks.id"],
            name="fk_artifact_task_dependency_bindings_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "dependency_kind IN ('assessment_product', 'artifact')",
            name="ck_artifact_dependency_kind",
        ),
        sa.CheckConstraint(
            "resolution_status IN ('bound', 'degraded', 'failed', 'timed_out', "
            "'canceled', 'omitted_after_wait')",
            name="ck_artifact_dependency_resolution_status",
        ),
    )
    op.create_index(
        "uq_artifact_dependency_product",
        "artifact_task_dependency_bindings",
        ["production_task_id", "dependency_key"],
        unique=True,
        postgresql_where=sa.text("dependency_kind = 'assessment_product'"),
    )
    op.create_index(
        "uq_artifact_dependency_artifact",
        "artifact_task_dependency_bindings",
        ["production_task_id", "dependency_key", "dependency_output_profile"],
        unique=True,
        postgresql_where=sa.text("dependency_kind = 'artifact'"),
    )

    op.create_table(
        "generated_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_key", sa.String(160), nullable=False),
        sa.Column("output_profile", sa.String(64), nullable=False),
        sa.Column("artifact_version", sa.String(128), nullable=False),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("production_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quality_grade", sa.String(16), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("publication_mode", sa.String(32), nullable=False),
        sa.Column("generation_reason", sa.Text(), nullable=True),
        sa.Column("marker", postgresql.JSONB(), nullable=True),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("template_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("data_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("render_manifest", postgresql.JSONB(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_generated_artifacts_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_task_id"],
            ["artifact_production_tasks.id"],
            name="fk_generated_artifacts_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            name="fk_generated_artifacts_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            name="fk_generated_artifacts_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "artifact_key",
            "output_profile",
            "artifact_version",
            name="uq_generated_artifact_version",
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'degraded', 'failed')",
            name="ck_generated_artifact_status",
        ),
        sa.CheckConstraint(
            "publication_mode IN ('automatic', 'rebuild', 'superadmin_override')",
            name="ck_generated_artifact_publication_mode",
        ),
    )
    op.create_index(
        "ix_generated_artifacts_run_id",
        "generated_artifacts",
        ["production_run_id"],
    )
    op.create_index(
        "ix_generated_artifacts_task_id",
        "generated_artifacts",
        ["production_task_id"],
    )
    op.create_index(
        "ix_generated_artifacts_event_id",
        "generated_artifacts",
        ["event_id"],
    )
    op.create_index(
        "uq_generated_artifact_final_task",
        "generated_artifacts",
        ["production_task_id"],
        unique=True,
        postgresql_where=sa.text("is_final"),
    )

    op.create_table(
        "artifact_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("production_mode", sa.String(32), nullable=False),
        sa.Column("artifact_key", sa.String(160), nullable=False),
        sa.Column("output_profile", sa.String(64), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("production_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_seq", sa.Integer(), nullable=False),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_forced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            name="fk_artifact_publications_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            name="fk_artifact_publications_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["generated_artifacts.id"],
            name="fk_artifact_publications_artifact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_artifact_publications_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artifact_publications_event_id",
        "artifact_publications",
        ["event_id"],
    )
    op.create_index(
        "ix_artifact_publications_artifact_id",
        "artifact_publications",
        ["artifact_id"],
    )
    op.create_index(
        "uq_artifact_publication_current",
        "artifact_publications",
        ["event_id", "artifact_key", "output_profile", "production_mode"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "artifact_override_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'processing'"),
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("production_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_artifact_override_requests_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["generated_artifacts.id"],
            name="fk_artifact_override_requests_artifact",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "endpoint",
            "idempotency_key",
            name="uq_artifact_override_request",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')",
            name="ck_artifact_override_request_status",
        ),
    )

    op.create_foreign_key(
        "fk_artifact_production_runs_input_snapshot",
        "artifact_production_runs",
        "production_input_snapshots",
        ["input_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_artifact_production_runs_rebuild_parent",
        "artifact_production_runs",
        "artifact_production_runs",
        ["rebuild_parent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_artifact_production_runs_superseded_by",
        "artifact_production_runs",
        "artifact_production_runs",
        ["superseded_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_artifact_production_tasks_final_artifact",
        "artifact_production_tasks",
        "generated_artifacts",
        ["final_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_generated_artifacts_superseded_by",
        "generated_artifacts",
        "generated_artifacts",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_generated_artifacts_superseded_by",
        "generated_artifacts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_artifact_production_tasks_final_artifact",
        "artifact_production_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_artifact_production_runs_superseded_by",
        "artifact_production_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_artifact_production_runs_rebuild_parent",
        "artifact_production_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_artifact_production_runs_input_snapshot",
        "artifact_production_runs",
        type_="foreignkey",
    )

    op.drop_table("artifact_override_requests")
    op.drop_table("artifact_publications")
    op.drop_table("artifact_task_dependency_bindings")
    op.drop_table("generated_artifacts")
    op.drop_table("artifact_production_tasks")
    op.drop_table("production_input_snapshot_items")
    op.drop_table("production_input_snapshots")
    op.drop_table("artifact_production_runs")
    op.drop_table("artifact_template_versions")
    op.drop_table("artifact_templates")

    postgresql.ENUM(
        "assessment_product",
        "artifact",
        name="artifact_dependency_kind",
    ).drop(op.get_bind(), checkfirst=True)

    op.alter_column(
        "assessment_runs",
        "t1_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
