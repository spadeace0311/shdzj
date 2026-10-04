"""Harden data asset snapshots and candidate child immutability."""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_data_asset_final_fixes"
down_revision: str | None = "0012_data_asset_center"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_data_asset_snapshot_reference()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            version_asset_id uuid;
            version_status text;
            version_name text;
            version_checksum text;
            version_region_id text;
            asset_key text;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'data_asset_snapshot_is_immutable'
                    USING ERRCODE = 'check_violation';
            END IF;

            SELECT version.asset_id,
                   version.status,
                   version.version,
                   version.checksum,
                   asset.region_id,
                   asset.asset_key
            INTO version_asset_id,
                 version_status,
                 version_name,
                 version_checksum,
                 version_region_id,
                 asset_key
            FROM data_asset_versions AS version
            JOIN data_assets AS asset ON asset.id = version.asset_id
            WHERE version.id = NEW.asset_version_id
            FOR KEY SHARE OF version, asset;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'data_asset_snapshot_version_not_found'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF version_status <> 'published' THEN
                RAISE EXCEPTION 'data_asset_snapshot_requires_published_version'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.asset_id IS DISTINCT FROM version_asset_id
                OR NEW.region_id IS DISTINCT FROM version_region_id
                OR NEW.asset_key IS DISTINCT FROM asset_key
                OR NEW.version IS DISTINCT FROM version_name
                OR NEW.checksum IS DISTINCT FROM version_checksum THEN
                RAISE EXCEPTION 'data_asset_snapshot_metadata_mismatch'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_data_asset_snapshots_validate_reference
        BEFORE INSERT OR UPDATE ON data_asset_snapshots
        FOR EACH ROW
        EXECUTE FUNCTION validate_data_asset_snapshot_reference()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_data_asset_child_mutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_version_id uuid;
            target_status text;
        BEGIN
            -- PostgreSQL invokes FK cascade actions at trigger depth > 1.
            -- Direct child mutations remain guarded even when the parent is
            -- being deleted.
            IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
                RETURN OLD;
            END IF;

            IF TG_OP = 'DELETE' THEN
                target_version_id := OLD.version_id;
            ELSE
                target_version_id := NEW.version_id;
            END IF;

            SELECT status
            INTO target_status
            FROM data_asset_versions
            WHERE id = target_version_id;

            -- A missing parent means this DELETE is part of ON DELETE CASCADE.
            IF FOUND AND target_status <> 'imported' THEN
                RAISE EXCEPTION 'data_asset_child_is_immutable'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    for table_name, trigger_name in (
        ("data_asset_records", "trg_data_asset_records_immutable"),
        ("data_asset_rasters", "trg_data_asset_rasters_immutable"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION enforce_data_asset_child_mutability()
            """
        )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_data_asset_rasters_immutable "
        "ON data_asset_rasters"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_data_asset_records_immutable "
        "ON data_asset_records"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS enforce_data_asset_child_mutability()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_data_asset_snapshots_validate_reference "
        "ON data_asset_snapshots"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS validate_data_asset_snapshot_reference()"
    )
