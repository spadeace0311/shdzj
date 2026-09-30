"""Allow missing production input snapshot items."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016_artifact_context_freeze"
down_revision: str | None = "0015_artifact_production"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
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
