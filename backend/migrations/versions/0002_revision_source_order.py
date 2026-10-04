"""Add source report ordering fields to earthquake revisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_revision_source_order"
down_revision: str | None = "0001_event_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "earthquake_revisions",
        sa.Column("source_report_time", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("source_report_number", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("earthquake_revisions", "source_report_number")
    op.drop_column("earthquake_revisions", "source_report_time")
