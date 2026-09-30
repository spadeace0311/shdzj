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
            PRIMARY KEY (id)
        )
        """
    )
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
            PRIMARY KEY (id)
        )
        """
    )
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
