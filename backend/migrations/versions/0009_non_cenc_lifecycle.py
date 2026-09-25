"""Use an explicit non-lifecycle state for manual, test, and drill events."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0009_non_cenc_lifecycle"
down_revision: str | None = "0008_region_boundaries_maritime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "earthquake_events",
        "lifecycle_state",
        existing_type=sa.String(length=32),
        server_default=sa.text("'not_applicable'"),
        existing_nullable=False,
    )
    op.execute(
        """
        UPDATE earthquake_events
        SET lifecycle_state = 'not_applicable',
            t1_at = NULL
        WHERE event_type IN ('manual', 'test', 'drill')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE earthquake_events
        SET lifecycle_state = 'auto_pending'
        WHERE lifecycle_state = 'not_applicable'
          AND event_type IN ('manual', 'test', 'drill')
        """
    )
    op.alter_column(
        "earthquake_events",
        "lifecycle_state",
        existing_type=sa.String(length=32),
        server_default=sa.text("'auto_pending'"),
        existing_nullable=False,
    )
