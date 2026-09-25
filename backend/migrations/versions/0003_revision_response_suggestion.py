"""Add response suggestion snapshot fields to earthquake revisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_revision_response_suggestion"
down_revision: str | None = "0002_revision_source_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "earthquake_revisions",
        sa.Column("institutional_level", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("service_level", sa.Integer(), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column(
            "response_suggestion",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("response_rule_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("earthquake_revisions", "response_rule_version")
    op.drop_column("earthquake_revisions", "response_suggestion")
    op.drop_column("earthquake_revisions", "service_level")
    op.drop_column("earthquake_revisions", "institutional_level")
