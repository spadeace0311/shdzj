"""Add loss assessment schema."""

from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Raster
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0014_loss_assessment"
down_revision: str | None = "0013_data_asset_final_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "loss_model_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.String(160), nullable=False),
        sa.Column("model_type", sa.String(48), nullable=False),
        sa.Column("formula_version", sa.String(128), nullable=False),
        sa.Column("applicable_region", sa.String(64), nullable=False),
        sa.Column("applicable_admin_levels", postgresql.JSONB(), nullable=False),
        sa.Column("input_contract", postgresql.JSONB(), nullable=False),
        sa.Column("output_contract", postgresql.JSONB(), nullable=False),
        sa.Column("source_citations", postgresql.JSONB(), nullable=False),
        sa.Column("calibration_status", sa.String(32), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_id",
            "formula_version",
            name="uq_loss_model_formula",
        ),
    )

    op.create_table(
        "loss_parameter_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parameter_set_id", sa.String(160), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(160), nullable=False),
        sa.Column("formula_version", sa.String(128), nullable=False),
        sa.Column("region_id", sa.String(64), nullable=False),
        sa.Column("calibration_status", sa.String(32), nullable=False),
        sa.Column("quality_grade", sa.String(16), nullable=False),
        sa.Column("scenarios", postgresql.JSONB(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parameter_set_id",
            "version",
            name="uq_loss_parameter_version",
        ),
    )

    op.create_table(
        "loss_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quality_grade", sa.String(16), nullable=False),
        sa.Column("calibration_status", sa.String(32), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column("partial_scope", sa.Boolean(), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("spatialized_estimate", sa.Boolean(), nullable=False),
        sa.Column("algorithm_version", sa.String(128), nullable=False),
        sa.Column("parameter_version", sa.String(128), nullable=False),
        sa.Column("region_profile_version", sa.String(128), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_checksum", sa.String(64), nullable=False),
        sa.Column("output_checksum", sa.String(64), nullable=False),
        sa.Column("statistics", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
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
            name="uq_loss_product_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('complete','partial','unavailable','invalid')",
            name="ck_loss_product_status_value",
        ),
        sa.CheckConstraint(
            "quality_grade IN ('L1','L2','L3','L0')",
            name="ck_loss_product_quality_value",
        ),
        sa.CheckConstraint(
            "product_type IN "
            "('building_damage','population_impact','casualties',"
            "'economic_loss','resource_demand','validation')",
            name="ck_loss_product_type",
        ),
    )

    op.create_table(
        "loss_metric_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("area_scope", sa.String(32), nullable=False),
        sa.Column("area_code", sa.String(64), nullable=False),
        sa.Column("area_name", sa.String(256), nullable=True),
        sa.Column("metric_key", sa.String(96), nullable=False),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("value_status", sa.String(32), nullable=False),
        sa.Column("numeric_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("precision", sa.Integer(), nullable=True),
        sa.Column("quality_grade", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["loss_products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "area_scope",
            "area_code",
            "metric_key",
            "value_type",
            name="uq_loss_metric_value",
        ),
        sa.CheckConstraint(
            "value_type IN ('low','central','high')",
            name="ck_loss_metric_value_type",
        ),
        sa.CheckConstraint(
            "value_status IN "
            "('available','zero','rounded_to_zero','unavailable','not_applicable')",
            name="ck_loss_metric_value_status",
        ),
        sa.CheckConstraint(
            """
            (value_status IN ('available','zero','rounded_to_zero')
             AND numeric_value IS NOT NULL AND numeric_value >= 0)
            OR
            (value_status IN ('unavailable','not_applicable')
             AND numeric_value IS NULL)
            """,
            name="ck_loss_metric_value_presence",
        ),
        sa.CheckConstraint(
            "(value_status = 'zero' AND numeric_value = 0) "
            "OR value_status <> 'zero'",
            name="ck_loss_metric_zero_value",
        ),
    )

    op.create_table(
        "loss_product_rasters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raster_version", sa.String(128), nullable=False),
        sa.Column("rast", Raster(), nullable=False),
        sa.Column("band_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("srid", sa.Integer(), nullable=False),
        sa.Column("spatial_allocation_rule", sa.String(128), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["loss_products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "raster_version",
            name="uq_loss_product_raster_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("loss_product_rasters")
    op.drop_table("loss_metric_values")
    op.drop_table("loss_products")
    op.drop_table("loss_parameter_sets")
    op.drop_table("loss_model_definitions")
