from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry, Raster
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_intensity_assessment"
down_revision: str | None = "0010_assessment_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis_raster")

    op.add_column(
        "assessment_runs",
        sa.Column("report_ingested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("deadline_basis_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("deadline_exceeded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("assessment_runs", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "assessment_runs",
        sa.Column("algorithm_bundle_version", sa.String(128), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("superseded_by_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE assessment_runs
        SET report_ingested_at = t1_at,
            deadline_basis_at = t1_at
        WHERE report_ingested_at IS NULL OR deadline_basis_at IS NULL
        """
    )
    op.alter_column("assessment_runs", "report_ingested_at", nullable=False)
    op.alter_column("assessment_runs", "deadline_basis_at", nullable=False)
    op.create_foreign_key(
        "fk_assessment_runs_superseded_by_run",
        "assessment_runs",
        "assessment_runs",
        ["superseded_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_assessment_runs_report_ingested_at",
        "assessment_runs",
        ["report_ingested_at"],
    )
    op.create_index(
        "ix_assessment_runs_deadline_basis_at",
        "assessment_runs",
        ["deadline_basis_at"],
    )
    op.create_index(
        "ix_assessment_runs_superseded_by_run_id",
        "assessment_runs",
        ["superseded_by_run_id"],
    )

    op.add_column(
        "assessment_tasks",
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "assessment_tasks",
        sa.Column("output_checksum", sa.String(64), nullable=True),
    )
    op.add_column(
        "assessment_tasks",
        sa.Column("algorithm_version", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_assessment_tasks_input_fingerprint",
        "assessment_tasks",
        ["input_fingerprint"],
    )

    op.create_table(
        "assessment_task_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["assessment_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_assessment_task_attempts_number",
        ),
    )
    op.create_index(
        "ix_assessment_task_attempts_status",
        "assessment_task_attempts",
        ["status"],
    )

    op.create_table(
        "intensity_field_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("algorithm_version", sa.String(128), nullable=False),
        sa.Column("parameter_version", sa.String(128), nullable=False),
        sa.Column("strategy_version", sa.String(128), nullable=True),
        sa.Column("grid_definition_version", sa.String(128), nullable=False),
        sa.Column("region_profile_version", sa.String(128), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_checksum", sa.String(64), nullable=False),
        sa.Column("output_checksum", sa.String(64), nullable=True),
        sa.Column("quality_grade", sa.String(16), nullable=True),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "spatial_extent",
            Geometry(geometry_type="POLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column("statistics", postgresql.JSONB(), nullable=False),
        sa.Column("source_product_id", sa.String(160), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assessment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["assessment_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "product_type",
            name="uq_intensity_product_run_type",
        ),
    )
    for name, columns in (
        ("ix_intensity_products_task_id", ["task_id"]),
        ("ix_intensity_products_product_type", ["product_type"]),
        ("ix_intensity_products_status", ["status"]),
        ("ix_intensity_products_quality_grade", ["quality_grade"]),
        ("ix_intensity_products_source_product_id", ["source_product_id"]),
    ):
        op.create_index(name, "intensity_field_products", columns)

    op.create_table(
        "intensity_rasters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rast", Raster(), nullable=False),
        sa.Column("band_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("srid", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["intensity_field_products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id"),
    )
    op.add_column(
        "earthquake_events",
        sa.Column(
            "latest_assessment_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "earthquake_events",
        sa.Column(
            "effective_assessment_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_earthquake_events_latest_assessment_run",
        "earthquake_events",
        "assessment_runs",
        ["latest_assessment_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_earthquake_events_effective_assessment_run",
        "earthquake_events",
        "assessment_runs",
        ["effective_assessment_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_earthquake_events_latest_assessment_run_id",
        "earthquake_events",
        ["latest_assessment_run_id"],
    )
    op.create_index(
        "ix_earthquake_events_effective_assessment_run_id",
        "earthquake_events",
        ["effective_assessment_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_earthquake_events_effective_assessment_run_id",
        table_name="earthquake_events",
    )
    op.drop_index(
        "ix_earthquake_events_latest_assessment_run_id",
        table_name="earthquake_events",
    )
    op.drop_constraint(
        "fk_earthquake_events_effective_assessment_run",
        "earthquake_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_earthquake_events_latest_assessment_run",
        "earthquake_events",
        type_="foreignkey",
    )
    op.drop_column("earthquake_events", "effective_assessment_run_id")
    op.drop_column("earthquake_events", "latest_assessment_run_id")

    op.drop_table("intensity_rasters")
    for name in (
        "ix_intensity_products_source_product_id",
        "ix_intensity_products_quality_grade",
        "ix_intensity_products_status",
        "ix_intensity_products_product_type",
        "ix_intensity_products_task_id",
    ):
        op.drop_index(name, table_name="intensity_field_products")
    op.drop_table("intensity_field_products")

    op.drop_index(
        "ix_assessment_task_attempts_status",
        table_name="assessment_task_attempts",
    )
    op.drop_table("assessment_task_attempts")

    op.drop_index(
        "ix_assessment_tasks_input_fingerprint",
        table_name="assessment_tasks",
    )
    op.drop_column("assessment_tasks", "algorithm_version")
    op.drop_column("assessment_tasks", "output_checksum")
    op.drop_column("assessment_tasks", "input_fingerprint")

    op.drop_index(
        "ix_assessment_runs_superseded_by_run_id",
        table_name="assessment_runs",
    )
    op.drop_index(
        "ix_assessment_runs_deadline_basis_at",
        table_name="assessment_runs",
    )
    op.drop_index(
        "ix_assessment_runs_report_ingested_at",
        table_name="assessment_runs",
    )
    op.drop_constraint(
        "fk_assessment_runs_superseded_by_run",
        "assessment_runs",
        type_="foreignkey",
    )
    op.drop_column("assessment_runs", "superseded_at")
    op.drop_column("assessment_runs", "superseded_by_run_id")
    op.drop_column("assessment_runs", "algorithm_bundle_version")
    op.drop_column("assessment_runs", "duration_ms")
    op.drop_column("assessment_runs", "deadline_exceeded_at")
    op.drop_column("assessment_runs", "deadline_basis_at")
    op.drop_column("assessment_runs", "report_ingested_at")
