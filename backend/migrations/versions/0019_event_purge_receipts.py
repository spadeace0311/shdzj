"""Add idempotent superadmin event purge receipts."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0019_event_purge_receipts"
down_revision: str | None = "0018_collaboration_command_hall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)
JSONB_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "event_purge_receipts",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("deletion_counts", JSONB_TYPE, nullable=False),
        sa.Column("storage_paths", JSONB_TYPE, nullable=False),
        sa.Column(
            "purged_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_event_purge_receipt_event_key",
        "event_purge_receipts",
        ["event_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_event_purge_receipts_purged_at",
        "event_purge_receipts",
        ["purged_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_purge_receipts_purged_at",
        table_name="event_purge_receipts",
    )
    op.drop_index(
        "uq_event_purge_receipt_event_key",
        table_name="event_purge_receipts",
    )
    op.drop_table("event_purge_receipts")
