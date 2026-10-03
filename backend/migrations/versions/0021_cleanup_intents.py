"""Persist object cleanup intents for post-commit retries."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0021_cleanup_intents"
down_revision: str | None = "0020_override_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "event_object_cleanup_intents",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'skipped')",
            name="ck_event_object_cleanup_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_kind",
            "source_key",
            "storage_path",
            name="uq_event_object_cleanup_source_path",
        ),
    )
    op.create_index(
        "ix_event_object_cleanup_event_id",
        "event_object_cleanup_intents",
        ["event_id"],
        unique=False,
    )
    op.create_index(
        "ix_event_object_cleanup_claim",
        "event_object_cleanup_intents",
        ["status", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_object_cleanup_claim",
        table_name="event_object_cleanup_intents",
    )
    op.drop_index(
        "ix_event_object_cleanup_event_id",
        table_name="event_object_cleanup_intents",
    )
    op.drop_table("event_object_cleanup_intents")
