"""Allow missing production input snapshot items."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016_artifact_context_freeze"
down_revision: str | None = "0015_artifact_production"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ARCHIVE_TABLE = "production_input_snapshot_items_missing"
_ARCHIVE_IDENTITY_CONSTRAINT = (
    "uq_production_input_snapshot_items_missing_snapshot_asset_role"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ARCHIVE_TABLE} (
            id UUID NOT NULL,
            snapshot_id UUID NOT NULL,
            asset_key VARCHAR(160) NOT NULL,
            asset_version_id UUID NULL,
            checksum VARCHAR(64) NULL,
            role VARCHAR(32) NOT NULL,
            coverage JSONB NOT NULL,
            selected_for_render BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (id),
            CONSTRAINT {_ARCHIVE_IDENTITY_CONSTRAINT}
                UNIQUE (snapshot_id, asset_key, role)
        )
        """
    )
    _ensure_archive_identity_constraint()
    op.alter_column(
        "production_input_snapshot_items",
        "asset_version_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "production_input_snapshot_items",
        "checksum",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.execute(
        f"""
        DELETE FROM {_ARCHIVE_TABLE} AS archive_row
        WHERE NOT EXISTS (
            SELECT 1
            FROM production_input_snapshots AS snapshot_row
            WHERE snapshot_row.id = archive_row.snapshot_id
        )
        """
    )
    op.execute(
        f"""
        INSERT INTO production_input_snapshot_items (
            id,
            snapshot_id,
            asset_key,
            asset_version_id,
            checksum,
            role,
            coverage,
            selected_for_render
        )
        SELECT
            id,
            snapshot_id,
            asset_key,
            asset_version_id,
            checksum,
            role,
            coverage,
            selected_for_render
        FROM {_ARCHIVE_TABLE}
        ON CONFLICT (snapshot_id, asset_key, role) DO NOTHING
        """
    )
    op.execute(f"DELETE FROM {_ARCHIVE_TABLE}")


def downgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ARCHIVE_TABLE} (
            id UUID NOT NULL,
            snapshot_id UUID NOT NULL,
            asset_key VARCHAR(160) NOT NULL,
            asset_version_id UUID NULL,
            checksum VARCHAR(64) NULL,
            role VARCHAR(32) NOT NULL,
            coverage JSONB NOT NULL,
            selected_for_render BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (id),
            CONSTRAINT {_ARCHIVE_IDENTITY_CONSTRAINT}
                UNIQUE (snapshot_id, asset_key, role)
        )
        """
    )
    _ensure_archive_identity_constraint()
    op.execute(
        f"""
        INSERT INTO {_ARCHIVE_TABLE} (
            id,
            snapshot_id,
            asset_key,
            asset_version_id,
            checksum,
            role,
            coverage,
            selected_for_render
        )
        SELECT
            id,
            snapshot_id,
            asset_key,
            asset_version_id,
            checksum,
            role,
            coverage,
            selected_for_render
        FROM production_input_snapshot_items
        WHERE asset_version_id IS NULL
           OR checksum IS NULL
        ON CONFLICT (snapshot_id, asset_key, role) DO UPDATE
        SET asset_version_id = EXCLUDED.asset_version_id,
            checksum = EXCLUDED.checksum,
            coverage = EXCLUDED.coverage,
            selected_for_render = EXCLUDED.selected_for_render
        """
    )
    op.execute(
        """
        DELETE FROM production_input_snapshot_items
        WHERE asset_version_id IS NULL
           OR checksum IS NULL
        """
    )
    op.alter_column(
        "production_input_snapshot_items",
        "checksum",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "production_input_snapshot_items",
        "asset_version_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )


def _ensure_archive_identity_constraint() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = '{_ARCHIVE_IDENTITY_CONSTRAINT}'
                  AND conrelid = '{_ARCHIVE_TABLE}'::regclass
            ) THEN
                ALTER TABLE {_ARCHIVE_TABLE}
                ADD CONSTRAINT {_ARCHIVE_IDENTITY_CONSTRAINT}
                UNIQUE (snapshot_id, asset_key, role);
            END IF;
        END;
        $$
        """
    )
