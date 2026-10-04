"""Add data asset center tables and run snapshot columns."""

from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry, Raster
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_data_asset_center"
down_revision: str | None = "0011_intensity_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_key", sa.String(160), nullable=False),
        sa.Column("region_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("data_type", sa.String(32), nullable=False),
        sa.Column("spatial_granularity", sa.String(32), nullable=False),
        sa.Column("responsibility_unit", sa.String(64), nullable=False),
        sa.Column("update_interval_days", sa.Integer(), nullable=False),
        sa.Column("is_core", sa.Boolean(), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
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
        sa.UniqueConstraint(
            "asset_key",
            "region_id",
            name="uq_data_assets_key_region",
        ),
    )

    op.create_table(
        "data_asset_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'imported'"),
            nullable=False,
        ),
        sa.Column("source_uri", sa.String(2048), nullable=False),
        sa.Column("license_name", sa.String(256), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality_grade", sa.String(16), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("schema_summary", postgresql.JSONB(), nullable=False),
        sa.Column(
            "record_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "spatial_extent",
            Geometry(geometry_type="POLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "source_crs",
            sa.String(16),
            server_default=sa.text("'EPSG:4326'"),
            nullable=False,
        ),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("managed_path", sa.String(1024), nullable=True),
        sa.Column("imported_by", sa.String(128), nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
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
            ["asset_id"],
            ["data_assets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asset_id",
            "version",
            name="uq_data_asset_versions_asset_version",
        ),
        sa.CheckConstraint(
            "status IN ('imported', 'validated', 'published', 'retired', 'rejected')",
            name="ck_data_asset_versions_status",
        ),
    )
    op.create_index(
        "ix_data_asset_versions_asset_id",
        "data_asset_versions",
        ["asset_id"],
    )
    op.create_index(
        "ix_data_asset_versions_status",
        "data_asset_versions",
        ["status"],
    )
    op.create_index(
        "ix_data_asset_versions_quality_grade",
        "data_asset_versions",
        ["quality_grade"],
    )
    op.create_index(
        "uq_data_asset_versions_published_asset",
        "data_asset_versions",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_data_asset_version_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            immutable_changed boolean;
        BEGIN
            immutable_changed :=
                OLD.asset_id IS DISTINCT FROM NEW.asset_id
                OR OLD.version IS DISTINCT FROM NEW.version
                OR OLD.source_uri IS DISTINCT FROM NEW.source_uri
                OR OLD.license_name IS DISTINCT FROM NEW.license_name
                OR OLD.acquired_at IS DISTINCT FROM NEW.acquired_at
                OR OLD.valid_from IS DISTINCT FROM NEW.valid_from
                OR OLD.valid_to IS DISTINCT FROM NEW.valid_to
                OR OLD.quality_grade IS DISTINCT FROM NEW.quality_grade
                OR OLD.change_note IS DISTINCT FROM NEW.change_note
                OR OLD.schema_summary IS DISTINCT FROM NEW.schema_summary
                OR OLD.record_count IS DISTINCT FROM NEW.record_count
                OR OLD.spatial_extent IS DISTINCT FROM NEW.spatial_extent
                OR OLD.source_crs IS DISTINCT FROM NEW.source_crs
                OR OLD.checksum IS DISTINCT FROM NEW.checksum
                OR OLD.managed_path IS DISTINCT FROM NEW.managed_path
                OR OLD.imported_by IS DISTINCT FROM NEW.imported_by
                OR OLD.imported_at IS DISTINCT FROM NEW.imported_at
                OR OLD.validated_at IS DISTINCT FROM NEW.validated_at
                OR OLD.created_at IS DISTINCT FROM NEW.created_at;

            IF NEW.status = OLD.status THEN
                IF OLD.status IN ('published', 'retired')
                    AND immutable_changed THEN
                    RAISE EXCEPTION 'data_asset_version_is_immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'published' AND NEW.status = 'retired' THEN
                IF immutable_changed THEN
                    RAISE EXCEPTION 'data_asset_version_is_immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'retired' AND NEW.status = 'published' THEN
                IF immutable_changed THEN
                    RAISE EXCEPTION 'data_asset_version_is_immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'imported'
                AND NEW.status IN ('validated', 'rejected') THEN
                RETURN NEW;
            END IF;

            IF OLD.status = 'validated' AND NEW.status = 'published' THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION
                'invalid data asset version status transition: % -> %',
                OLD.status,
                NEW.status
                USING ERRCODE = 'check_violation';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_data_asset_versions_enforce_lifecycle
        BEFORE UPDATE ON data_asset_versions
        FOR EACH ROW
        EXECUTE FUNCTION enforce_data_asset_version_lifecycle()
        """
    )

    op.create_table(
        "data_asset_import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_format", sa.String(32), nullable=False),
        sa.Column("source_uri", sa.String(2048), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("raw_checksum", sa.String(64), nullable=False),
        sa.Column("managed_path", sa.String(1024), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "validation_errors",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "validation_warnings",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "statistics",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["data_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"],
            ["data_asset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_data_asset_import_jobs_asset_id",
        "data_asset_import_jobs",
        ["asset_id"],
    )
    op.create_index(
        "ix_data_asset_import_jobs_asset_version_id",
        "data_asset_import_jobs",
        ["asset_version_id"],
    )
    op.create_index(
        "ix_data_asset_import_jobs_status",
        "data_asset_import_jobs",
        ["status"],
    )

    op.create_table(
        "data_asset_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("region_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_key", sa.String(160), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["assessment_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["data_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"],
            ["data_asset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "region_id",
            "asset_key",
            name="uq_data_asset_snapshots_run_region_asset",
        ),
    )
    op.create_index(
        "ix_data_asset_snapshots_run_id",
        "data_asset_snapshots",
        ["run_id"],
    )
    op.create_index(
        "ix_data_asset_snapshots_asset_id",
        "data_asset_snapshots",
        ["asset_id"],
    )
    op.create_index(
        "ix_data_asset_snapshots_region_id",
        "data_asset_snapshots",
        ["region_id"],
    )
    op.create_index(
        "ix_data_asset_snapshots_asset_version_id",
        "data_asset_snapshots",
        ["asset_version_id"],
    )

    op.create_table(
        "data_asset_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["data_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["data_asset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_data_asset_audit_logs_asset_id",
        "data_asset_audit_logs",
        ["asset_id"],
    )
    op.create_index(
        "ix_data_asset_audit_logs_version_id",
        "data_asset_audit_logs",
        ["version_id"],
    )

    op.create_table(
        "data_asset_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("business_key", sa.String(512), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column(
            "geom",
            Geometry(geometry_type="GEOMETRY", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["data_asset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "row_number",
            name="uq_data_asset_records_version_row",
        ),
    )
    op.create_index(
        "ix_data_asset_records_version_id",
        "data_asset_records",
        ["version_id"],
    )
    op.create_index(
        "ix_data_asset_records_geom",
        "data_asset_records",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_data_asset_records_properties",
        "data_asset_records",
        ["properties"],
        postgresql_using="gin",
    )

    op.create_table(
        "data_asset_rasters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rast", Raster(), nullable=False),
        sa.Column("band_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("srid", sa.Integer(), nullable=False),
        sa.Column(
            "spatial_extent",
            Geometry(geometry_type="POLYGON", srid=4326),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["data_asset_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", name="uq_data_asset_rasters_version"),
    )

    op.add_column(
        "assessment_runs",
        sa.Column("data_asset_snapshot_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "assessment_runs",
        sa.Column("data_asset_snapshot_result", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_assessment_runs_data_asset_snapshot_fingerprint",
        "assessment_runs",
        ["data_asset_snapshot_fingerprint"],
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_data_asset_versions_enforce_lifecycle "
        "ON data_asset_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_data_asset_version_lifecycle()")

    op.drop_index(
        "ix_assessment_runs_data_asset_snapshot_fingerprint",
        table_name="assessment_runs",
    )
    op.drop_column("assessment_runs", "data_asset_snapshot_result")
    op.drop_column("assessment_runs", "data_asset_snapshot_fingerprint")

    op.drop_table("data_asset_rasters")

    op.drop_index(
        "ix_data_asset_records_properties",
        table_name="data_asset_records",
    )
    op.drop_index("ix_data_asset_records_geom", table_name="data_asset_records")
    op.drop_index(
        "ix_data_asset_records_version_id",
        table_name="data_asset_records",
    )
    op.drop_table("data_asset_records")

    op.drop_index(
        "ix_data_asset_audit_logs_version_id",
        table_name="data_asset_audit_logs",
    )
    op.drop_index(
        "ix_data_asset_audit_logs_asset_id",
        table_name="data_asset_audit_logs",
    )
    op.drop_table("data_asset_audit_logs")

    op.execute("DROP INDEX IF EXISTS ix_data_asset_snapshots_region_id")
    op.drop_index(
        "ix_data_asset_snapshots_asset_version_id",
        table_name="data_asset_snapshots",
    )
    op.drop_index(
        "ix_data_asset_snapshots_asset_id",
        table_name="data_asset_snapshots",
    )
    op.drop_index(
        "ix_data_asset_snapshots_run_id",
        table_name="data_asset_snapshots",
    )
    op.drop_table("data_asset_snapshots")

    op.drop_index(
        "ix_data_asset_import_jobs_status",
        table_name="data_asset_import_jobs",
    )
    op.drop_index(
        "ix_data_asset_import_jobs_asset_version_id",
        table_name="data_asset_import_jobs",
    )
    op.drop_index(
        "ix_data_asset_import_jobs_asset_id",
        table_name="data_asset_import_jobs",
    )
    op.drop_table("data_asset_import_jobs")

    op.drop_index(
        "uq_data_asset_versions_published_asset",
        table_name="data_asset_versions",
    )
    op.drop_index(
        "ix_data_asset_versions_quality_grade",
        table_name="data_asset_versions",
    )
    op.drop_index("ix_data_asset_versions_status", table_name="data_asset_versions")
    op.drop_index("ix_data_asset_versions_asset_id", table_name="data_asset_versions")
    op.drop_table("data_asset_versions")

    op.drop_table("data_assets")
